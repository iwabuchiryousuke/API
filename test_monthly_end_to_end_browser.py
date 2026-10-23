"""Real Chromium DOM -> monthly runner -> immutable records/live rows/CSV.

Every browser request is intercepted with anonymous local HTML. SBI login,
Gmail and PDF acquisition are injected; this is not a live SBI verification.
"""
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from browser_test_support import browser_executable
from ledger import Store
from monthly_records import load_measurements
from monthly_runner import run_monthly
from monthly_summary import capture_summary
from sbi_views import init_observations
from test_data_compatibility import seed_fixture as _seed_fixture


JST = timezone(timedelta(hours=9))
END = datetime(2026, 10, 31, 16, 3, 11, tzinfo=JST)
START = datetime(2026, 11, 1, 16, 8, 22, tzinfo=JST)
OWNERS = {account: 'Example ' + account for account in 'ABCD'}
PROTECTED = ('accounts', 'imports', 'trades', 'evidence', 'activities',
             'activity_files', 'balance_reports', 'asset_snapshots')


class MonthlyEndToEndBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        cls.playwright = sync_playwright().start()
        executable = browser_executable(cls.playwright)
        if executable is None:
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium is not installed')
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        live, _ = _seed_fixture(self.base)
        init_observations(live)
        live.close()
        self.enterContext(patch.dict('family_profile.OWNERS', OWNERS, clear=True))
        self.current = END
        self.calls = []
        self.requests = []
        self.wrong_owner = False
        self.authenticate = Mock(side_effect=lambda directory, account:
                                 (object(), account.lower() + '@example.test'))
        self.original = self.protected_rows()

    def protected_rows(self):
        live = Store(self.base / 'data')
        try:
            return {table: sorted(tuple(row) for row in live.db.execute('SELECT * FROM ' + table))
                    for table in PROTECTED}
        finally:
            live.close()

    def observation_rows(self):
        live = Store(self.base / 'data')
        try:
            return [dict(row) for row in live.db.execute(
                'SELECT account,kind,observed_at,payload FROM account_observations ORDER BY account,observed_at')]
        finally:
            live.close()

    def fixture(self, account):
        ordinal = ord(account) - ord('A')
        month_start = self.current.date() == START.date()
        stocks = 1000 + 100 * ordinal + (100 if month_start else 0)
        cash = 200 + 10 * ordinal + (30 if month_start else 0)
        owner = OWNERS['B'] if self.wrong_owner and account == 'A' else OWNERS[account]
        shown = self.current.strftime('%Y/%m/%d') + ' 15:58:00'
        nisa = ''
        if account == 'A':
            remaining = 1999000 if month_start else 2000000
            nisa = ('<table><tr><td>投資可能枠(2026年)-NISA(成長投資枠)</td>'
                    f'<td>{remaining:,}円</td></tr>'
                    '<tr><td>投資可能枠(2026年)-NISA(つみたて投資枠)</td>'
                    '<td>0円</td></tr></table>')
        return f'''<!doctype html><html lang="ja"><meta charset="utf-8">
          <p>ログアウト 口座管理 口座サマリー {owner} 様</p>
          <table><tr><td>| 保有資産評価<br>詳細</td></tr>
          <tr><td>現金残高等（合計）</td><td>{cash:,}</td></tr>
          <tr><td>SBI証券口座分</td><td>50</td></tr>
          <tr><td>スィープ専用銀行口座</td><td>{cash-50:,}</td></tr>
          <tr><td>株式</td><td>{stocks:,}</td></tr>
          <tr><td>計</td><td>{stocks+cash:,}</td></tr></table>
          <h2>ポートフォリオ</h2><table><tr><td>株式</td><td>999,999</td></tr></table>
          {nisa}<p>{shown}</p></html>'''

    def acquire(self, playwright, account, stage=None, **options):
        self.assertIs(playwright, self.playwright)
        self.assertTrue(options['headless'])
        if options['documents']:
            self.assertFalse(options['capture_assets'])
            self.calls.append(('pdf', account))
            return {'complete': True, 'pdf_collected': True, 'saved': 1, 'duplicate': 0}
        self.calls.append(('asset', account))
        self.assertTrue(options['summary_only'])
        self.assertTrue(options['capture_assets'])
        html = self.fixture(account)
        context = self.browser.new_context()
        try:
            # No request is allowed to reach SBI or any other external host.
            context.route('**/*', lambda route:
                          route.fulfill(body=html, content_type='text/html; charset=utf-8'))
            context.on('request', lambda request: self.requests.append(request.url))
            page = context.new_page()
            page.goto('https://site3.sbisec.co.jp/summary/' + account)
            with patch('sbi_views._jst_now', return_value=self.current):
                summary = capture_summary(page, stage, account, owner=OWNERS[account],
                                          identity_verified=True,
                                          progress=options['progress'], cancelled=options['cancelled'])
            return {'complete': True, 'assets_observed': summary['complete'],
                    'assets_missing': summary['missing'], 'pdf_collected': False,
                    'monthly_summary': summary, 'saved': 0, 'duplicate': 0}
        finally:
            context.close()

    def run_slot(self):
        return run_monthly(self.base, now=self.current, clock=lambda: self.current,
                           authenticate=self.authenticate, acquire=self.acquire,
                           playwright=self.playwright)

    def test_rendered_month_end_and_start_reach_records_live_rows_and_comparison_csv(self):
        month_end = self.run_slot()
        self.assertEqual(month_end['exit_code'], 0, month_end)
        self.assertEqual(self.calls, [('asset', account) for account in 'ABCD'])
        self.current = START
        month_start = self.run_slot()
        self.assertEqual(month_start['exit_code'], 0, month_start)
        self.assertEqual(self.calls[4:], [('asset', account) for account in 'ABCD'] +
                         [('pdf', account) for account in 'ABCD'])
        self.assertEqual(self.authenticate.call_count, 8)
        measurements = load_measurements(self.base, owners=OWNERS)
        self.assertEqual(len(measurements), 8)
        for record in measurements:
            actual = END if record['slot']['role'] == 'month-end' else START
            self.assertEqual(record['summary']['observed_at'], actual.isoformat())
            self.assertEqual(record['summary']['displayed_at'], actual.strftime('%Y/%m/%d') + ' 15:58:00')
            self.assertNotEqual(record['summary']['observed_at'], record['slot']['scheduled_for'])
            self.assertTrue(record['summary']['complete'])
            self.assertFalse(record['displayed_at_is_valuation'])
        observed = self.observation_rows()
        self.assertEqual(len(observed), 8)
        for row in observed:
            self.assertEqual(row['kind'], '口座サマリー')
            self.assertIn(json.loads(row['payload'])['values']['summary_stocks'],
                          (1000, 1100, 1200, 1300, 1400))
        value = month_start['comparison']
        self.assertIn('運用損益ではありません', value['note'])
        for row in value['accounts'].values():
            self.assertEqual(row['deltas']['summary_stocks'], 100)
            self.assertEqual(row['deltas']['cash_total'], 30)
        self.assertEqual(value['accounts']['A']['deltas']['growth_remaining'], -1000)
        self.assertEqual(value['accounts']['A']['deltas']['tsumitate_remaining'], 0)
        self.assertIsNone(value['accounts']['B']['deltas']['growth_remaining'])
        with Path(month_start['comparison_csv']).open(encoding='utf-8-sig', newline='') as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(len(csv_rows), 4)
        self.assertTrue(all(float(row['株式+現金増減(円)']) == 130 for row in csv_rows))
        for account in 'ABCD':
            marker = self.base / 'data/scheduler/pdf_completions/2026-11-01' / (account + '.json')
            self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['saved'], 1)
        self.assertEqual(self.protected_rows(), self.original)
        self.assertEqual(set(self.requests), {'https://site3.sbisec.co.jp/summary/' + a for a in 'ABCD'})
        immutable = {path: path.read_bytes() for path in
                     (self.base / 'data/monthly/measurements').rglob('*.json')}
        self.calls.clear()
        self.current += timedelta(minutes=1)
        repeated = self.run_slot()
        self.assertEqual(repeated['exit_code'], 0, repeated)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.authenticate.call_count, 8)
        self.assertEqual(self.observation_rows(), observed)
        self.assertEqual({path: path.read_bytes() for path in immutable}, immutable)

    def test_wrong_rendered_owner_writes_no_a_record_and_keeps_independent_other_accounts(self):
        self.wrong_owner = True
        result = self.run_slot()
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.calls, [('asset', account) for account in 'ABCD'])
        self.assertEqual([row['account'] for row in self.observation_rows()], list('BCD'))
        measurements = load_measurements(self.base, owners=OWNERS)
        self.assertEqual([record['account'] for record in measurements], list('BCD'))
        self.assertEqual(self.protected_rows(), self.original)
        self.assertFalse(list((self.base / 'data/monthly/measurements/A').glob('*.json')))


if __name__ == '__main__':
    unittest.main()
