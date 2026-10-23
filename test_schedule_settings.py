"""Local schedule editing preserves deletion intent and all financial files."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from schedule_settings import (ScheduleSettingsError, defaults, delete_schedule,
    get_schedule, load_settings, read_registration_status, restore_defaults,
    save_schedule, settings_digest, settings_path, validate_settings)


class ScheduleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_read_only_default_projection_has_two_enabled_entries_without_files(self):
        value = load_settings(self.base)
        self.assertEqual([item['id'] for item in value['schedules']], ['month-end', 'month-start'])
        self.assertEqual([item['include_pdfs'] for item in value['schedules']], [False, True])
        self.assertTrue(all(item['enabled'] and item['time']=='16:00' for item in value['schedules']))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_first_create_is_idempotent_and_keeps_full_schema(self):
        first = load_settings(self.base, create=True)
        original = settings_path(self.base).read_bytes()
        self.assertEqual(load_settings(self.base, create=True), first)
        self.assertEqual(settings_path(self.base).read_bytes(), original)

    def test_builtin_edit_time_pdf_enabled_and_revision_persist(self):
        old = get_schedule(self.base, 'month-start')
        changed = save_schedule(self.base, dict(old, time='18:35', include_pdfs=False, enabled=False), expected_revision=1)
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(get_schedule(self.base, 'month-start'), changed)
        self.assertFalse(changed['enabled'])
        self.assertEqual(load_settings(self.base)['revision'], 2)

    def test_deleted_builtin_is_tombstone_not_recreated_by_create(self):
        deleted = delete_schedule(self.base, 'month-end')
        self.assertTrue(deleted['deleted'])
        self.assertFalse(deleted['enabled'])
        for _ in range(3):
            self.assertTrue(load_settings(self.base, create=True)['schedules'][0]['deleted'])

    def test_once_accepts_aware_datetime_normalizes_to_jst_and_keeps_unique_ids(self):
        first = save_schedule(self.base, dict(kind='once', label='夜の確認', run_at='2026-10-23T11:30:00+00:00'))
        second = save_schedule(self.base, dict(kind='once', label='追加', run_at=datetime.fromisoformat('2026-10-24T16:20:00+09:00')))
        self.assertTrue(first['id'].startswith('once_'))
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual((first['time'], first['run_at']), ('20:30', '2026-10-23T20:30:00+09:00'))
        self.assertEqual(get_schedule(self.base, second['id']), second)

    def test_stale_edit_is_rejected_without_overwriting_newer_config(self):
        old = get_schedule(self.base, 'month-end')
        save_schedule(self.base, dict(old, time='17:00'), expected_revision=1)
        protected = settings_path(self.base).read_bytes()
        with self.assertRaises(ScheduleSettingsError):
            save_schedule(self.base, dict(old, time='18:00'), expected_revision=1)
        self.assertEqual(settings_path(self.base).read_bytes(), protected)

    def test_restore_is_explicit_and_preserves_custom_reservations(self):
        extra = save_schedule(self.base, {'run_at':'2026-11-05T18:00:00+09:00'})
        delete_schedule(self.base, 'month-end')
        restore_defaults(self.base)
        current = get_schedule(self.base, 'month-end')
        self.assertFalse(current['deleted']); self.assertTrue(current['enabled'])
        self.assertEqual(current['time'], '16:00')
        self.assertEqual(get_schedule(self.base, extra['id']), extra)

    def test_invalid_times_dates_unknown_keys_and_builtin_kind_are_rejected(self):
        for updates in ({'time':'24:00'}, {'time':'9:00'}, {'kind':'once'},
                        {'enabled':1}, {'label':'line\nbreak'}, {'label':'x'*81},
                        {'unknown':'data'}, {'deleted':True, 'enabled':True}):
            with self.subTest(updates=updates):
                current = get_schedule(self.base, 'month-end')
                with self.assertRaises(ScheduleSettingsError):
                    save_schedule(self.base, dict(current, **updates))
        for stamp in ('2026-02-30T16:00:00+09:00', '2026-10-23T16:00:00',
                      '2026-10-23T16:00:01+09:00'):
            with self.subTest(stamp=stamp), self.assertRaises(ScheduleSettingsError):
                save_schedule(self.base, {'run_at':stamp})

    def test_duplicate_ids_missing_builtin_or_bool_revision_are_rejected(self):
        value = defaults()
        for bad in (dict(value, schedules=[value['schedules'][0]]*2),
                    dict(value, schedules=value['schedules'][:1]),
                    dict(value, revision=True)):
            with self.assertRaises(ScheduleSettingsError): validate_settings(bad)

    def test_corrupt_json_and_duplicate_keys_are_preserved(self):
        load_settings(self.base, create=True)
        path = settings_path(self.base)
        for raw in (b'not json', b'{"schema":1,"schema":2}'):
            path.write_bytes(raw)
            with self.assertRaises(ScheduleSettingsError): load_settings(self.base, create=True)
            self.assertEqual(path.read_bytes(), raw)

    def test_settings_link_is_refused_without_touching_destination(self):
        load_settings(self.base, create=True)
        target = self.base/'protected.txt';target.write_text('keep')
        path=settings_path(self.base);path.unlink()
        try: path.symlink_to(target)
        except OSError: self.skipTest('Symlinks are not available')
        with self.assertRaises(ScheduleSettingsError): load_settings(self.base)
        self.assertEqual(target.read_text(), 'keep')

    def test_financial_and_private_files_are_unchanged_during_all_edit_operations(self):
        data=self.base/'data';data.mkdir()
        files={data/'ledger.sqlite3':b'opaque ledger', data/'login_private.enc':b'opaque encrypted settings'}
        for path, raw in files.items(): path.write_bytes(raw)
        load_settings(self.base,create=True)
        custom=save_schedule(self.base,{'run_at':'2026-11-02T17:00:00+09:00'})
        delete_schedule(self.base,custom['id']);restore_defaults(self.base)
        for path, raw in files.items(): self.assertEqual(path.read_bytes(),raw)

    def test_digest_changes_for_pdf_flag_time_and_revision(self):
        initial=settings_digest(load_settings(self.base))
        item=get_schedule(self.base,'month-end')
        save_schedule(self.base,dict(item,time='17:10',include_pdfs=True))
        self.assertNotEqual(initial,settings_digest(load_settings(self.base)))

    def test_registration_status_reads_only_known_fields_and_fixed_messages(self):
        self.assertIsNone(read_registration_status(self.base))
        load_settings(self.base,create=True)
        path=settings_path(self.base).with_name('task_registration.json')
        path.write_text(json.dumps(dict(status='failed',settings_revision=2,logon_mode='InteractiveToken',
            updated_at='2026-10-23T20:00:00+09:00',message='untrusted arbitrary content')))
        value=read_registration_status(self.base)
        self.assertEqual(value['status'],'failed')
        self.assertNotIn('untrusted',value['message'])


if __name__=='__main__': unittest.main()
