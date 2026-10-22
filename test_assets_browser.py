"""Real Chromium against intercepted synthetic SBI pages, never a live SBI login."""
import shutil
import tempfile
import unittest
from pathlib import Path

from ledger import Store
from login_credentials import CredentialError
from sbi_views import (snapshot_dom, parse_my_assets, include_cash,
                       capture_account, capture_status, latest, _jst_now, navigate, select_monthly_view)


class AssetBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        cls.playwright = sync_playwright().start()
        from browser_test_support import browser_executable
        executable = browser_executable(cls.playwright)
        if executable is None:
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium browser is not installed')
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self.context.close()

    def html(self, value):
        self.context.route('**/*', lambda route: route.fulfill(body=value, content_type='text/html; charset=utf-8'))
        self.page.goto('https://site.sbisec.co.jp/account/assets')

    def test_accessible_grid_uses_evaluation_header_ignores_foreign_frame(self):
        page = '''<nav>国内株式<br>外国株式<br>投資信託</nav>
        <div role="table"><div role="row"><span role="columnheader">商品</span>
        <span role="columnheader">取得額</span><span role="columnheader">評価額(円)</span></div>
        <div role="row"><span role="cell">外国株式(米国)</span>
        <span role="cell">2,000</span><span role="cell">3,000</span></div></div>
        <iframe src="https://untrusted.example/frame"></iframe>'''
        self.context.route('**/*', lambda route: route.fulfill(
            body=page if 'sbisec.co.jp' in route.request.url else '<p>外国株式(米国)</p><p>999円</p>',
            content_type='text/html; charset=utf-8'))
        self.page.goto('https://site.sbisec.co.jp/account/assets')
        text, tables = snapshot_dom(self.page)
        self.assertNotIn('999円', text)
        self.assertEqual(parse_my_assets(text, tables)['foreign'], 3000)

    def test_cash_exclusion_checkbox_is_cleared(self):
        self.html('<label><input type="checkbox" checked>預り金を除外</label>')
        include_cash(self.page)
        self.assertFalse(self.page.get_by_role('checkbox', name='預り金を除外').is_checked())

    def test_complete_asset_workflow_with_same_verified_session(self):
        from urllib.parse import urlparse
        day = _jst_now().date().isoformat().replace('-', '/')
        year = _jst_now().year
        nav = '''<nav><a href="/summary">口座管理</a><a href="/summary">口座(円建)</a>
        <a href="/assets">My資産</a><a href="/foreign">口座(外貨建)</a></nav>'''
        bodies = {
            '/home': '<p>架空 太郎 様 ログアウト</p>',
            '/summary': f'''<p>口座サマリー</p><table>
            <tr><td>投資可能枠({year}年)－NISA(成長投資枠)</td><td>0</td></tr>
            <tr><td>投資可能枠({year}年)－NISA(つみたて投資枠)</td><td>1,200,000</td></tr>
            <tr><td>現金残高等（合計）</td><td>4,000</td></tr>
            <tr><td>SBI証券口座分</td><td>1,000</td></tr>
            <tr><td>スィープ専用銀行口座</td><td>3,000</td></tr></table>''',
            '/assets': f'''<p>My資産</p><p>更新日時 {day} 12:00</p><table>
            <tr><th>商品</th><th>評価額(円)</th><th>評価損益</th></tr>
            <tr><td>国内株式(現物)</td><td>5,000</td><td>+2,000</td></tr>
            <tr><td>外国株式(米国)</td><td>2,000</td><td>+500</td></tr>
            <tr><td>預り金(米ドル)</td><td>1,500</td><td>--</td></tr></table>''',
            '/foreign': '''<table><tr><th>通貨</th><th>買付余力</th><th>預り金</th><th>円換算額</th></tr>
            <tr><td>米ドル</td><td>999</td><td>10.25</td><td>1,500</td></tr></table>'''
        }
        self.context.route('**/*', lambda route: route.fulfill(
            body='<html><body>' + nav + bodies.get(urlparse(route.request.url).path, '') + '</body></html>',
            content_type='text/html; charset=utf-8'))
        self.page.goto('https://site.sbisec.co.jp/home')
        complete = capture_account(self.page, self.store, 'A', owner='架空太郎')
        self.assertTrue(complete)
        status = capture_status(self.store, 'A')
        self.assertTrue(status['complete'])
        self.assertEqual(len(status['available']), 6)
        values = latest(self.store, '2099-01-01')[('A', 'My資産')]['values']
        self.assertEqual(values['usd_cash'], 10.25)
        self.assertEqual(values['foreign_cash'], 1500)
        self.assertEqual(values['foreign'], 2000)

    def test_asset_navigation_follows_new_trusted_sbi_tab(self):
        self.html('<a target="_blank" href="https://site.sbisec.co.jp/account/assets">My資産</a>')
        opened = navigate(self.page, ['My資産'])
        self.assertIsNot(opened, self.page)
        self.assertIs(opened.context, self.context)
        self.assertTrue(opened.url.startswith('https://site.sbisec.co.jp/'))

    def test_monthly_toggle_selects_monthly_instead_of_daily(self):
        self.html('''<button onclick="document.body.dataset.daily='yes'">前日比/率(%)</button>
        <button onclick="document.body.dataset.monthly='yes'">前月比/率(%)</button>''')
        select_monthly_view(self.page)
        self.assertEqual(self.page.locator('body').get_attribute('data-monthly'), 'yes')
        self.assertIsNone(self.page.locator('body').get_attribute('data-daily'))

    def test_unknown_identity_never_saves_observation(self):
        self.html('<p>ログアウト 口座管理</p><p>国内株式(現物)</p><p>5,000円</p>')
        with self.assertRaises(CredentialError):
            capture_account(self.page, self.store, 'A', owner='架空太郎')
        self.assertEqual(latest(self.store, '2099-01-01'), {})


if __name__ == '__main__':
    unittest.main()
