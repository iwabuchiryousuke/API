"""Configured acquisition evidence, with synthetic owners and no database."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest

from scheduled_run_store import (ScheduledRecordError, ScheduledRunStore,
                                 clear_account_attention, occurrence_key)


def plan(**changes):
    value = dict(schedule_id='once-abc', schedule_revision=1, kind='once',
                 scheduled_for='2026-10-23T10:15:00+09:00', slot_date='2026-10-23',
                 include_pdfs=True, comparison_month=None)
    value.update(changes)
    return value


def summary(**changes):
    value = dict(complete=True, missing=[],
                 values=dict(summary_stocks=1000, cash_total=50, broker_cash=20,
                             bank=30, summary_total=1050),
                 observed_at='2026-10-23T10:17:00+09:00', displayed_at='2026/10/23 10:14:00')
    value.update(changes)
    return value


class ScheduledRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.owners = {'A': 'Example A', 'B': 'Example B'}
        self.store = ScheduledRunStore(self.base, plan(), self.owners)

    def test_constructor_and_absent_preflight_are_read_only(self):
        self.store.preflight()
        self.assertIsNone(self.store.select('A'))
        self.assertFalse(self.store.pdf_done('A'))
        self.assertFalse(self.store.held('A'))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_once_before_16_records_actual_time_without_creating_a_monthly_value(self):
        opaque = self.base / 'data/private/opaque.dat'
        opaque.parent.mkdir(parents=True)
        opaque.write_bytes(b'synthetic-opaque-settings')
        path = self.store.save('A', summary())
        record = self.store.select('A')
        self.assertTrue(path.is_file())
        self.assertEqual(record['summary']['observed_at'], '2026-10-23T10:17:00+09:00')
        self.assertEqual(record['summary']['displayed_at'], '2026/10/23 10:14:00')
        self.assertEqual(record['timing'], 'near-schedule')
        self.assertFalse(record['displayed_at_is_valuation'])
        self.assertEqual(record['owner'], 'ExampleA')
        self.assertEqual(opaque.read_bytes(), b'synthetic-opaque-settings')
        self.assertFalse((self.base / 'data/monthly').exists())
        self.assertFalse((self.base / 'data/ledger.sqlite3').exists())

    def test_recurring_custom_time_accepts_correct_calendar_day(self):
        monthly = plan(schedule_id='month-end', kind='month-end',
                       scheduled_for='2026-10-31T17:45:00+09:00',
                       slot_date='2026-10-31', comparison_month='2026-11')
        store = ScheduledRunStore(self.base, monthly, self.owners)
        store.save('A', summary(observed_at='2026-10-31T17:48:00+09:00'))
        self.assertEqual(store.select('A')['plan']['scheduled_for'], monthly['scheduled_for'])
        self.assertIsNone(self.store.select('A'))

    def test_invalid_plans_never_create_files(self):
        invalid = [plan(schedule_id='../outside'), plan(schedule_revision=True),
                   plan(schedule_revision=0), plan(kind='daily'), plan(include_pdfs='yes'),
                   plan(scheduled_for='2026-10-23T10:15:03+09:00'),
                   plan(scheduled_for='2026-10-23T10:15:00'),
                   plan(slot_date='2026-10-24'), plan(comparison_month='2026-10'),
                   plan(kind='month-end', comparison_month='2026-11'),
                   plan(kind='month-start', comparison_month='2026-10')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ScheduledRecordError):
                ScheduledRunStore(self.base, value, self.owners)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_plan_identity_changes_for_revision_time_or_pdf_policy(self):
        base = occurrence_key(plan())
        for changed in (plan(schedule_revision=2), plan(include_pdfs=False),
                        plan(scheduled_for='2026-10-23T10:16:00+09:00')):
            self.assertNotEqual(occurrence_key(changed), base)

    def test_attempts_are_immutable_and_choose_first_complete_without_field_mixing(self):
        partial = summary(complete=False, missing=['summary_stocks'],
                          values=dict(cash_total=70), observed_at='2026-10-23T10:16:00+09:00')
        first = self.store.save('A', partial)
        before = first.read_bytes()
        second = self.store.save('A', summary(observed_at='2026-10-23T10:20:00+09:00'))
        third = self.store.save('A', summary(observed_at='2026-10-23T10:19:00+09:00'))
        self.assertEqual(first.read_bytes(), before)
        self.assertNotEqual(second, third)
        selected = self.store.select('A')
        self.assertEqual(selected['summary']['observed_at'], '2026-10-23T10:19:00+09:00')
        self.assertEqual(selected['summary']['values']['cash_total'], 50)
        self.assertEqual(len(list(first.parent.glob('*.json'))), 3)

    def test_partial_and_explicit_zero_are_different(self):
        self.store.save('A', summary(complete=False, missing=['summary_stocks'], values={'cash_total': 0}))
        result = self.store.select('A')['summary']
        self.assertIsNone(result['values']['summary_stocks'])
        self.assertEqual(result['values']['cash_total'], 0)
        self.assertFalse(result['complete'])

    def test_late_same_day_is_preserved_but_early_or_next_day_is_rejected(self):
        self.store.save('A', summary(observed_at='2026-10-23T10:46:00+09:00'))
        self.assertEqual(self.store.select('A')['timing'], 'late')
        for stamp in ('2026-10-23T10:14:59+09:00', '2026-10-24T10:17:00+09:00'):
            with self.subTest(stamp=stamp), self.assertRaises(ScheduledRecordError):
                self.store.save('A', summary(observed_at=stamp))

    def test_bad_financial_values_cannot_become_completion(self):
        for values in ({'cash_total': 50, 'summary_stocks': float('nan')},
                       {'cash_total': 50, 'summary_stocks': 100, 'summary_total': 100},
                       {'cash_total': 50, 'summary_stocks': 100, 'broker_cash': 20, 'bank': 40}):
            with self.subTest(values=values), self.assertRaises(ScheduledRecordError):
                self.store.save('A', summary(values=values))
        self.assertIsNone(self.store.select('A'))

    def test_owner_change_cannot_rebind_saved_evidence(self):
        self.store.save('A', summary())
        other = ScheduledRunStore(self.base, plan(), {'A': 'Changed owner', 'B': 'Example B'})
        with self.assertRaises(ScheduledRecordError):
            other.preflight()
        self.assertEqual(self.store.select('A')['owner'], 'ExampleA')

    def test_corrupt_record_stops_preflight_even_when_another_attempt_is_complete(self):
        bad = self.store.save('A', summary())
        self.store.save('A', summary(observed_at='2026-10-23T10:18:00+09:00'))
        value = json.loads(bad.read_text())
        value['summary']['values']['summary_stocks'] = 9000
        bad.write_text(json.dumps(value))
        corrupted = bad.read_bytes()
        with self.assertRaises(ScheduledRecordError):
            self.store.preflight()
        self.assertEqual(bad.read_bytes(), corrupted)

    def test_valid_checksum_record_cannot_be_copied_to_another_account(self):
        source = self.store.save('A', summary())
        other = source.parent.parent / 'B'
        other.mkdir()
        copied = other / source.name
        copied.write_bytes(source.read_bytes())
        with self.assertRaises(ScheduledRecordError):
            self.store.preflight()

    def test_hardlinked_measurement_is_not_accepted(self):
        path = self.store.save('A', summary())
        try:
            os.link(path, self.base / 'linked.json')
        except OSError:
            self.skipTest('Hardlink creation unavailable')
        with self.assertRaises(ScheduledRecordError):
            self.store.select('A')

    def test_duplicate_json_key_cannot_hide_modified_financial_value(self):
        path = self.store.save('A', summary())
        raw = path.read_text()
        path.write_text(raw.replace('"version":1', '"version":1,"version":1'))
        with self.assertRaises(ScheduledRecordError):
            self.store.select('A')

    def test_pdf_marker_requires_verified_completion_and_is_idempotent(self):
        outcome = dict(complete=True, pdf_collected=True, saved=3, duplicate=2)
        path = self.store.save_pdf('A', outcome, datetime.fromisoformat('2026-10-23T10:22:00+09:00'))
        content = path.read_bytes()
        self.assertTrue(self.store.pdf_done('A'))
        self.assertEqual(self.store.save_pdf('A', outcome, '2026-10-23T10:23:00+09:00'), path)
        self.assertEqual(path.read_bytes(), content)
        revised = ScheduledRunStore(self.base, plan(schedule_revision=2), self.owners)
        self.assertFalse(revised.pdf_done('A'))

    def test_partial_negative_or_wrong_day_pdf_is_not_completed(self):
        bad = [dict(complete=False, pdf_collected=True), dict(complete=True, pdf_collected=False),
               dict(complete=True, pdf_collected=True, saved=-1),
               dict(complete=True, pdf_collected=True, duplicate=True)]
        for outcome in bad:
            with self.subTest(outcome=outcome), self.assertRaises(ScheduledRecordError):
                self.store.save_pdf('A', outcome, '2026-10-23T10:22:00+09:00')
        with self.assertRaises(ScheduledRecordError):
            self.store.save_pdf('A', dict(complete=True, pdf_collected=True), '2026-10-24T10:22:00+09:00')
        self.assertFalse(self.store.pdf_done('A'))

    def test_corrupt_pdf_marker_is_retained_and_blocks_preflight(self):
        path = self.store.save_pdf('A', dict(complete=True, pdf_collected=True), '2026-10-23T10:22:00+09:00')
        path.write_bytes(path.read_bytes().replace(b'"saved":0', b'"saved":1'))
        before = path.read_bytes()
        with self.assertRaises(ScheduledRecordError):
            self.store.preflight()
        self.assertEqual(path.read_bytes(), before)

    def test_manual_hold_is_account_and_occurrence_bound_and_fixed_metadata_only(self):
        path = self.store.hold('A', 'NEEDS_CONSENT')
        self.assertTrue(self.store.held('A'))
        self.assertFalse(self.store.held('B'))
        self.assertFalse(ScheduledRunStore(self.base, plan(schedule_revision=2), self.owners).held('A'))
        value = json.loads(path.read_text())
        self.assertEqual(value['code'], 'NEEDS_CONSENT')
        self.assertNotIn('error', value)
        self.assertNotIn('password', path.read_text().lower())
        with self.assertRaises(ScheduledRecordError):
            self.store.hold('B', 'arbitrary raw error text')

    def test_explicit_test_clear_validates_all_owners_before_any_removal(self):
        first = self.store.hold('A')
        revised = ScheduledRunStore(self.base, plan(schedule_revision=2), self.owners)
        second = revised.hold('A')
        other = self.store.hold('B')
        with self.assertRaises(ScheduledRecordError):
            clear_account_attention(self.base, 'A', 'Changed owner')
        self.assertTrue(first.exists() and second.exists() and other.exists())
        self.assertEqual(clear_account_attention(self.base, 'A', 'Example A'), 2)
        self.assertFalse(first.exists() or second.exists())
        self.assertTrue(other.exists())

    def test_corrupt_hold_is_not_cleared_and_prevents_partial_clear(self):
        first = self.store.hold('A')
        second = ScheduledRunStore(self.base, plan(schedule_revision=2), self.owners).hold('A')
        second.write_bytes(second.read_bytes().replace(b'"version":1', b'"version":2'))
        with self.assertRaises(ScheduledRecordError):
            self.store.clear_attention('A')
        self.assertTrue(first.exists() and second.exists())

    def test_symlink_directory_and_marker_cannot_escape_private_tree(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.base / 'data').mkdir()
        try:
            (self.base / 'data/scheduled').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ScheduledRecordError):
            self.store.save('A', summary())
        self.assertEqual(list(outside.iterdir()), [])

    def test_marker_symlink_is_not_followed_or_removed(self):
        path = self.store.hold('A')
        outside = self.base / 'outside.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        before = outside.read_bytes()
        with self.assertRaises(ScheduledRecordError):
            self.store.preflight()
        with self.assertRaises(ScheduledRecordError):
            self.store.clear_attention('A')
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(path.is_symlink())

    def test_unknown_account_and_unknown_owner_map_are_rejected(self):
        with self.assertRaises(ScheduledRecordError):
            self.store.save('C', summary())
        with self.assertRaises(ScheduledRecordError):
            ScheduledRunStore(self.base, plan(), {'E': 'Example E'})


if __name__ == '__main__':
    unittest.main()
