"""Adversarial monthly-runner boundaries with synthetic data and I/O only."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from ledger import Store
from monthly_records import load_measurements
from test_monthly_runner_integration import MonthlyRunnerFixture, MONTH_END


class MonthlyRunnerSafety(MonthlyRunnerFixture):
    def test_first_unattended_start_requires_full_upgrade_backup_before_auth(self):
        with patch('data_backup.backup_before_upgrade', side_effect=OSError('Synthetic backup failure')):
            result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.observations(), [])
        self.assertEqual(self.protected_rows(), self.before)
        self.current += timedelta(minutes=1)
        recovered = self.run_monthly()
        self.assertEqual(recovered['exit_code'], 0)
        backup = Path(recovered['upgrade_backup'])
        self.assertEqual((backup / 'data/private/opaque.dat').read_bytes(), self.private.read_bytes())
        self.assertTrue((backup / 'manifest.json').is_file())

    def test_live_owner_change_during_capture_blocks_commit_but_retains_bound_evidence(self):
        original = self.acquire

        def change_owner(playwright, account, stage=None, **options):
            result = original(playwright, account, stage, **options)
            if account == 'A' and not options['documents']:
                live = Store(self.base / 'data')
                try:
                    live.set_owner('A', 'Changed synthetic owner')
                finally:
                    live.close()
            return result

        self.current = MONTH_END
        result = self.run_monthly(acquire=change_owner)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual([row['account'] for row in self.observations()], list('BCD'))
        records = load_measurements(self.base, accounts=('A',), owners={'A': 'Example A'})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['owner'], 'ExampleA')
        live = Store(self.base / 'data')
        try:
            self.assertEqual(next(row for row in live.accounts() if row['id'] == 'A')['owner_name'],
                             'Changed synthetic owner')
        finally:
            live.close()

    def test_corrupt_pdf_marker_stops_before_auth_and_failure_is_reported(self):
        self.assertEqual(self.run_monthly()['exit_code'], 0)
        marker = self.base / 'data/scheduler/pdf_completions' / self.current.date().isoformat() / 'A.json'
        value = json.loads(marker.read_text(encoding='utf-8'))
        value['saved'] += 1
        marker.write_text(json.dumps(value), encoding='utf-8')
        corrupt = marker.read_bytes()
        self.calls.clear()
        self.auth_calls.clear()
        self.current += timedelta(minutes=1)
        result = self.run_monthly()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(marker.read_bytes(), corrupt)
        self.assertIsNotNone(result['run_path'])
        report = json.loads(Path(result['run_path']).read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'failed')
        self.assertNotEqual(report['exit_code'], 0)

    def test_future_observations_are_never_fresh_or_backdated(self):
        original = self.acquire

        def future(playwright, account, stage=None, **options):
            real_time = self.current
            self.current += timedelta(days=1)
            try:
                return original(playwright, account, stage, **options)
            finally:
                self.current = real_time

        self.current = MONTH_END
        result = self.run_monthly(acquire=future)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.observations(), [])
        self.assertEqual(load_measurements(self.base), [])
        self.assertEqual(self.protected_rows(), self.before)

    def test_crossing_midnight_cannot_complete_the_previous_calendar_slot(self):
        self.current = MONTH_END.replace(hour=23, minute=59)
        original = self.acquire

        def cross_midnight(playwright, account, stage=None, **options):
            if account == 'A' and not options['documents']:
                self.current += timedelta(minutes=2)
            return original(playwright, account, stage, **options)

        result = self.run_monthly(acquire=cross_midnight)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.observations(), [])
        self.assertEqual(load_measurements(self.base), [])
        self.assertEqual(result['slot']['slot_date'], '2026-10-31')
        self.assertEqual(self.protected_rows(), self.before)

    def test_cli_dry_run_reads_no_settings_and_writes_no_files(self):
        from monthly_runner import main
        with tempfile.TemporaryDirectory() as directory:
            with patch('monthly_runner.BASE', Path(directory)), \
                 patch('monthly_runner.Store', side_effect=AssertionError('Dry-run must not open the ledger')), \
                 patch('gmail_auth.service', side_effect=AssertionError('Dry-run must not authenticate')), \
                 redirect_stdout(io.StringIO()) as output:
                code = main(['--dry-run'])
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(json.loads(output.getvalue())['planning_only'])

    def test_comparison_csv_escapes_formula_labels_and_keeps_numeric_values(self):
        import csv
        self.assertEqual(self.run_monthly(include_pdfs=False)['exit_code'], 0)
        from monthly_runner import export_comparison
        from monthly_records import comparison
        value = comparison(self.base, '2026-11', {a: 'Example ' + a for a in 'ABCD'})
        path = export_comparison(self.base, value,
                                 {'A': '=HYPERLINK("https://example.invalid")',
                                  'B': '+SUM(1,2)', 'C': '-A1', 'D': '@SUM(A1:A2)'})
        with Path(path).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual([row[2][0] for row in rows[1:]], ["'"] * 4)
        self.assertTrue(rows[1][9].replace('.', '', 1).isdigit())
        self.assertEqual(self.protected_rows(), self.before)
