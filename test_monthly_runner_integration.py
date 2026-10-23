"""Monthly automation uses synthetic accounts, credentials and observations only.

These tests exercise the public runner. They never launch a browser, access
Gmail, decrypt settings, or touch uploaded financial data.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ledger import Store
from login_credentials import CredentialError
from sbi_views import init_observations, save_observation
from test_data_compatibility import seed_fixture
from update_workflow import exclusive


JST = timezone(timedelta(hours=9))
MONTH_START = datetime(2026, 11, 1, 16, 7, tzinfo=JST)
MONTH_END = datetime(2026, 10, 31, 16, 7, tzinfo=JST)
PROTECTED = ('accounts', 'imports', 'trades', 'evidence', 'activities',
             'activity_files', 'balance_reports', 'asset_snapshots',
             'user_preferences')


class MonthlyRunnerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        store, _ = seed_fixture(self.base)
        init_observations(store)
        with store.db:
            store.db.execute('CREATE TABLE user_preferences(name TEXT PRIMARY KEY,value TEXT)')
            store.db.execute('INSERT INTO user_preferences VALUES (?,?)', ('layout', 'keep'))
        store.close()
        self.private = self.base / 'data/private/opaque.dat'
        self.private.parent.mkdir(parents=True)
        self.private.write_bytes(b'opaque-synthetic-private-setting')
        self.current = MONTH_START
        self.calls = []
        self.auth_calls = []
        self.asset_failures = set()
        self.pdf_failures = set()
        self.write_fresh = True
        self.partial = False
        self.mutate_stage = False
        self.cancel_after_asset = False
        self.was_cancelled = False
        self.before = self.protected_rows()

    def protected_rows(self):
        store = Store(self.base / 'data')
        try:
            return {table: sorted(tuple(row) for row in store.db.execute('SELECT * FROM ' + table))
                    for table in PROTECTED}
        finally:
            store.close()

    def observations(self):
        store = Store(self.base / 'data')
        try:
            return [dict(row) for row in store.db.execute(
                'SELECT account,kind,observed_at,payload FROM account_observations ORDER BY account,observed_at')]
        finally:
            store.close()

    def authenticate(self, directory, account):
        self.assertEqual(Path(directory), self.base / 'data')
        self.auth_calls.append(account)
        return object(), account.lower() + '@example.test'

    def acquire(self, playwright, account, stage=None, **options):
        self.assertIs(playwright, self.playwright)
        self.assertEqual(options['gmail_session'][1], account.lower() + '@example.test')
        self.assertTrue(options['headless'])
        if options['documents']:
            self.assertFalse(options['capture_assets'])
            self.calls.append(('pdf', account))
            if account in self.pdf_failures:
                raise CredentialError('Synthetic PDF failure')
            return {'complete': True, 'pdf_collected': True, 'saved': 0, 'duplicate': 0}
        self.assertTrue(options['capture_assets'])
        self.assertTrue(options['summary_only'])
        self.assertEqual(options['target_dates'], ())
        self.assertIsInstance(stage, Store)
        self.calls.append(('asset', account))
        if account in self.asset_failures:
            raise CredentialError('Synthetic identity or acquisition failure')
        if self.write_fresh:
            ordinal = ord(account) - ord('A')
            values = {'cash_total': 50 + ordinal, 'broker_cash': 20,
                      'bank': 30 + ordinal}
            if not self.partial:
                values.update(summary_stocks=1000 + ordinal,
                              summary_total=1050 + ordinal * 2)
            with patch('sbi_views._jst_now', return_value=self.current):
                save_observation(stage, account, '口座サマリー',
                                 self.current.strftime('%Y/%m/%d %H:%M:%S'), values)
            if self.mutate_stage:
                with stage.db:
                    stage.db.execute('UPDATE accounts SET label=? WHERE id=?', ('Stage-only label', account))
                    stage.db.execute('UPDATE user_preferences SET value=?', ('Stage-only preference',))
                    stage.db.execute('UPDATE asset_snapshots SET payload=? WHERE account=?',
                                     (json.dumps({'source': 'Stage-only', 'cash': 999999}), 'A'))
        if self.cancel_after_asset:
            self.was_cancelled = True
        # The public result is deliberately insufficient as financial evidence.
        # The runner must inspect fresh accepted rows in the staging store.
        return {'complete': True, 'assets_observed': True, 'pdf_collected': False,
                'assets_missing': [], 'saved': 0, 'duplicate': 0}

    def run_monthly(self, **overrides):
        from monthly_runner import run_monthly
        self.playwright = getattr(self, 'playwright', Mock(name='synthetic-playwright'))
        options = dict(now=self.current, authenticate=self.authenticate,
                       acquire=self.acquire, playwright=self.playwright,
                       clock=lambda: self.current,
                       cancelled=lambda: self.was_cancelled)
        options.update(overrides)
        return run_monthly(self.base, **options)


class MonthlyRunnerIntegration(MonthlyRunnerFixture):
    def test_wrong_day_and_before_16_do_not_open_settings_or_create_a_ledger(self):
        from monthly_runner import run_monthly
        authenticate = Mock(side_effect=AssertionError('No authentication is due'))
        acquire = Mock(side_effect=AssertionError('No browser work is due'))
        for now in (datetime(2026, 11, 2, 16, 7, tzinfo=JST),
                    MONTH_START.replace(hour=15, minute=59)):
            with self.subTest(now=now), tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / 'unconfigured'
                result = run_monthly(base, now=now, authenticate=authenticate, acquire=acquire)
                self.assertEqual(result['status'], 'skipped')
                self.assertEqual(result['exit_code'], 0)
                self.assertFalse((base / 'data/ledger.sqlite3').exists())
        authenticate.assert_not_called()
        acquire.assert_not_called()

    def test_month_start_captures_all_assets_before_any_pdf_without_applying_trades(self):
        self.mutate_stage = True
        result = self.run_monthly()
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'] + [('pdf', a) for a in 'ABCD'])
        self.assertEqual(set(result['accounts']), set('ABCD'))
        self.assertEqual(self.protected_rows(), self.before)
        self.assertEqual(self.private.read_bytes(), b'opaque-synthetic-private-setting')
        rows = self.observations()
        self.assertEqual([row['account'] for row in rows], list('ABCD'))
        for row in rows:
            payload = json.loads(row['payload'])
            self.assertEqual(payload['observed_at'], self.current.isoformat())
            self.assertEqual(row['kind'], '口座サマリー')
            self.assertIn('summary_stocks', payload['values'])
            self.assertIn('cash_total', payload['values'])
        from monthly_records import load_measurements
        measurements = load_measurements(self.base, owners={a: 'Example ' + a for a in 'ABCD'})
        self.assertEqual(len(measurements), 4)
        for measurement in measurements:
            self.assertEqual(measurement['summary']['observed_at'], self.current.isoformat())
            self.assertNotEqual(measurement['summary']['observed_at'], measurement['slot']['scheduled_for'])
            self.assertFalse(measurement['displayed_at_is_valuation'])

    def test_month_end_never_acquires_pdfs(self):
        self.current = MONTH_END
        result = self.run_monthly()
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])

    def test_failure_in_one_account_keeps_other_measurements_and_pdf_phase(self):
        self.asset_failures = {'A'}
        result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'] + [('pdf', a) for a in 'ABCD'])
        self.assertEqual([row['account'] for row in self.observations()], list('BCD'))
        self.assertEqual(self.protected_rows(), self.before)
        self.calls.clear()
        self.asset_failures.clear()
        self.current += timedelta(minutes=1)
        retry = self.run_monthly()
        self.assertEqual(retry['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', 'A')])
        self.assertEqual([row['account'] for row in self.observations()], list('ABCD'))

    def test_completed_measurements_and_successful_pdf_phases_are_idempotent(self):
        first = self.run_monthly()
        self.assertEqual(first['exit_code'], 0)
        original = self.observations()
        self.calls.clear()
        self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        second = self.run_monthly()
        self.assertEqual(second['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.observations(), original)

    def test_failed_pdf_retries_only_that_pdf_without_recapturing_assets(self):
        self.pdf_failures = {'C'}
        first = self.run_monthly()
        self.assertNotEqual(first['exit_code'], 0)
        self.assertEqual(len(self.observations()), 4)
        self.calls.clear()
        self.pdf_failures.clear()
        self.current += timedelta(minutes=1)
        retry = self.run_monthly()
        self.assertEqual(retry['exit_code'], 0)
        self.assertEqual(self.calls, [('pdf', 'C')])

    def test_no_fresh_row_cannot_be_certified_by_complete_return_value(self):
        store = Store(self.base / 'data')
        with patch('sbi_views._jst_now', return_value=self.current - timedelta(hours=1)):
            for account in 'ABCD':
                save_observation(store, account, '口座サマリー', '',
                                 {'summary_stocks': 1000, 'cash_total': 50})
        store.close()
        before = self.observations()
        self.write_fresh = False
        result = self.run_monthly(include_pdfs=False)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.observations(), before)
        self.calls.clear()
        self.current += timedelta(minutes=1)
        self.run_monthly(include_pdfs=False)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])

    def test_partial_summary_is_not_a_completed_slot_and_unknown_stock_is_not_zero(self):
        self.partial = True
        first = self.run_monthly(include_pdfs=False)
        self.assertNotEqual(first['exit_code'], 0)
        for row in self.observations():
            self.assertNotIn('summary_stocks', json.loads(row['payload'])['values'])
        self.calls.clear()
        self.partial = False
        self.current += timedelta(minutes=1)
        second = self.run_monthly(include_pdfs=False)
        self.assertEqual(second['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])

    def test_test_mode_does_not_consume_production_measurement_or_pdf_markers(self):
        tested = self.run_monthly(test=True)
        self.assertEqual(tested['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])
        self.assertIn('test_runs', Path(tested['run_path']).parts)
        self.calls.clear()
        self.current += timedelta(minutes=1)
        production = self.run_monthly()
        self.assertEqual(production['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'] + [('pdf', a) for a in 'ABCD'])

    def test_test_mode_can_measure_now_on_an_ordinary_day_before_16(self):
        self.current = datetime(2026, 10, 23, 10, 5, tzinfo=JST)
        result = self.run_monthly(test=True)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])
        self.assertIn('test_runs', Path(result['run_path']).parts)
        self.calls.clear()
        regular = self.run_monthly()
        self.assertEqual(regular['status'], 'skipped')
        self.assertEqual(self.calls, [])

    def test_changed_owner_cannot_reuse_or_silently_overwrite_a_completed_measurement(self):
        self.assertEqual(self.run_monthly()['exit_code'], 0)
        store = Store(self.base / 'data')
        store.set_owner('A', 'Different synthetic owner')
        store.close()
        self.calls.clear()
        self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        current = Store(self.base / 'data')
        try:
            self.assertEqual(next(row for row in current.accounts() if row['id'] == 'A')['owner_name'],
                             'Different synthetic owner')
        finally:
            current.close()

    def test_background_authentication_never_starts_interactive_consent(self):
        with patch('gmail_auth.service', side_effect=self.authenticate) as service, \
             patch('gmail_auth.ensure_service', side_effect=AssertionError('No interactive consent')) as consent:
            result = self.run_monthly(authenticate=None, include_pdfs=False)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(service.call_count, 4)
        consent.assert_not_called()

    def test_existing_workflow_lock_prevents_any_account_access(self):
        with exclusive(self.base / 'data/update.lock'):
            result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.protected_rows(), self.before)

    def test_durable_measurements_recover_after_live_backup_failure_without_new_login(self):
        self.current = MONTH_END
        original_backup = Store.backup

        def backup(store):
            if store.directory == self.base / 'data':
                raise OSError('Synthetic live backup disk-full failure')
            return original_backup(store)

        with patch.object(Store, 'backup', autospec=True, side_effect=backup):
            first = self.run_monthly()
        self.assertNotEqual(first['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', a) for a in 'ABCD'])
        self.assertEqual(self.protected_rows(), self.before)
        self.assertEqual(self.observations(), [])
        self.calls.clear()
        self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        recovered = self.run_monthly()
        self.assertEqual(recovered['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        self.assertEqual([row['account'] for row in self.observations()], list('ABCD'))

    def test_cancellation_during_first_capture_stops_all_following_accounts_and_pdfs(self):
        self.cancel_after_asset = True
        result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', 'A')])
        self.assertEqual(self.protected_rows(), self.before)


if __name__ == '__main__':
    unittest.main()
