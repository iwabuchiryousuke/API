"""Real Chromium for current cash/NISA only; every request is intercepted."""
from contextlib import ExitStack
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from browser_test_support import browser_executable
from current_asset_reader import capture_current, read_current
from ledger import Store
from login_credentials import CredentialError
from sbi_views import latest
from test_current_asset_reader import NOW


class CurrentAssetBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        cls.playwright=sync_playwright().start()
        executable=browser_executable(cls.playwright)
        if executable is None:
            cls.playwright.stop();raise unittest.SkipTest('Chromium is not installed')
        cls.browser=cls.playwright.chromium.launch(executable_path=executable,headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.context=self.browser.new_context();self.page=self.context.new_page()
        self.temporary=tempfile.TemporaryDirectory();self.store=Store(self.temporary.name)
        self.requests=[]
        self.enterContext(patch.dict('family_profile.OWNERS',{'A':'Example A','B':'Example B'},clear=True))
        self.enterContext(patch('sbi_views._jst_now',return_value=NOW))

    def tearDown(self):
        self.context.close();self.store.close();self.temporary.cleanup()

    def route(self,summary=None,foreign=None,owner='Example A'):
        nav='''<nav><a href="/summary">口座管理</a><a href="/summary">口座(円建)</a>
            <a href="/foreign">口座(外貨建)</a><a href="/assets">My資産</a></nav>'''
        summary=summary if summary is not None else '''<h2>口座サマリー</h2><table>
            <tr><td>現金残高等（合計）</td><td>4,000円</td></tr>
            <tr><td>SBI証券口座分</td><td>1,000円</td></tr>
            <tr><td>スィープ専用銀行口座</td><td>3,000円</td></tr>
            <tr><td>投資可能枠（2026年）－NISA（成長投資枠）</td><td>0円</td></tr>
            <tr><td>投資可能枠（2026年）－NISA（つみたて投資枠）</td><td>1,200,000円</td></tr></table>'''
        foreign=foreign if foreign is not None else '''<table><tr><th>通貨</th><th>買付余力</th><th>預り金</th><th>円換算額</th></tr>
            <tr><td>米ドル</td><td>999</td><td>12.25</td><td>1,800</td></tr></table>'''
        bodies={'/summary':summary,'/foreign':foreign,'/hidden':'<p>米ドル残高 999 USD</p>'}
        def response(route):
            path=urlparse(route.request.url).path;self.requests.append(path)
            content=bodies.get(path,'')
            header=f'<p>{owner} 様 ログアウト</p>' if path!='/hidden' else ''
            route.fulfill(body='<html><body>'+header+nav+content+'</body></html>',content_type='text/html; charset=utf-8')
        self.context.route('**/*',response)
        self.page.goto('https://site.sbisec.co.jp/home')

    def capture(self,**options):
        # Short bounded waits keep incomplete-fixture tests fast while still
        # exercising real delayed DOM changes and stabilization.
        def bounded(page,kind,**kwargs):
            return read_current(page,kind,timeout=2.8,stable_seconds=.3,**kwargs)
        with patch('current_asset_reader.read_current',side_effect=bounded):
            return capture_current(self.page,self.store,'A',owner='Example A',identity_verified=True,**options)

    def test_current_summary_and_usd_capture_never_visits_my_assets_or_history(self):
        self.route()
        result=self.capture()
        self.assertTrue(result['complete']);self.assertEqual(result['missing'],[])
        self.assertEqual(result['values']['cash_total'],4000)
        self.assertEqual(result['values']['usd_cash'],12.25)
        self.assertEqual(result['values']['growth_remaining']['remaining'],0)
        observations=latest(self.store,'2099-01-01')
        self.assertEqual(set(observations),{('A','口座サマリー'),('A','口座(外貨建)')})
        self.assertEqual(observations['A','口座(外貨建)']['values'],{'usd_cash':12.25})
        self.assertNotIn('/assets',self.requests)
        self.assertFalse(any('history' in path or 'portfolio' in path for path in self.requests))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_snapshots'").fetchone())

    def test_delayed_nisa_is_waited_for_without_replacing_cash(self):
        self.route(summary='''<p>現金残高等合計 4,000円</p><div id="nisa"></div>
          <script>setTimeout(()=>document.getElementById('nisa').innerHTML=
          '<p>投資可能枠(2026年)－NISA(成長投資枠) 2,400円</p><p>投資可能枠(2026年)－NISA(つみたて投資枠) 1,200,000円</p>',800)</script>''')
        result=self.capture()
        self.assertTrue(result['complete'])
        self.assertEqual(result['values']['growth_remaining']['remaining'],2400)

    def test_later_usd_cash_is_waited_for_after_yen_valuation_only(self):
        self.route(foreign='''<div id="balance"><p>外貨現金(円) 1,800円</p></div>
          <script>setTimeout(()=>document.getElementById('balance').innerHTML=
          '<table><tr><th>通貨</th><th>預り金</th><th>円換算額</th></tr><tr><td>USD</td><td>12.25</td><td>1800</td></tr></table>',900)</script>''')
        result=self.capture()
        self.assertTrue(result['complete']);self.assertEqual(result['values']['usd_cash'],12.25)
        self.assertNotIn('foreign_cash',result['values'])

    def test_only_buying_power_never_becomes_native_cash_or_zero(self):
        self.route(foreign='''<table><tr><th>通貨</th><th>買付余力</th><th>円換算額</th></tr>
             <tr><td>米ドル</td><td>999</td><td>1800</td></tr></table>''')
        result=self.capture()
        self.assertFalse(result['complete']);self.assertEqual(result['missing'],['usd_cash'])
        self.assertNotIn('usd_cash',result['values'])
        self.assertNotIn(('A','口座(外貨建)'),latest(self.store,'2099-01-01'))

    def test_hidden_same_host_frame_does_not_replace_visible_usd(self):
        self.route(foreign='''<p>米ドル残高 12.25 USD</p><iframe style="display:none" src="/hidden"></iframe>''')
        result=self.capture()
        self.assertTrue(result['complete']);self.assertEqual(result['values']['usd_cash'],12.25)

    def test_readonly_zero_balance_control_reveals_actual_zero_without_inference(self):
        self.route(foreign='''<label><input type="checkbox" onchange="document.getElementById('zero').hidden=!this.checked">残高ゼロの通貨を表示</label>
            <p id="zero" hidden>米ドル現金残高 0 USD</p>''')
        result=self.capture()
        self.assertTrue(result['complete']);self.assertEqual(result['values']['usd_cash'],0)
        self.assertEqual(latest(self.store,'2099-01-01')['A','口座(外貨建)']['values'],{'usd_cash':0})

    def test_old_selected_year_is_missing_even_though_its_number_is_visible(self):
        self.route(summary='''<p>現金残高等合計 4,000円</p>
            <p>投資可能枠(2025年)－NISA(成長投資枠) 999円</p>
            <p>投資可能枠(2025年)－NISA(つみたて投資枠) 999円</p>''')
        result=self.capture()
        self.assertEqual(result['missing'],['growth_remaining','tsumitate_remaining'])
        self.assertNotIn('growth_remaining',result['values'])

    def test_wrong_owner_stops_without_current_observations(self):
        self.route(owner='Example B')
        with self.assertRaises(CredentialError):self.capture()
        self.assertEqual(latest(self.store,'2099-01-01'),{})

    def test_cancellation_stops_before_any_asset_page_or_write(self):
        self.route()
        before=list(self.requests)
        with self.assertRaises(CredentialError):self.capture(cancelled=lambda:True)
        self.assertEqual(self.requests,before)
        self.assertEqual(latest(self.store,'2099-01-01'),{})
