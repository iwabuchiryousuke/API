"""Real Chromium, all routes intercepted; current SBI screenshot-shaped fixtures."""
import tempfile
import unittest
from urllib.parse import urlparse
from unittest.mock import patch

from ledger import Store,LedgerError
from login_credentials import CredentialError
from sbi_views import (select_asset_page,capture_account,capture_status,latest,navigate,
                       include_cash,parse_summary,parse_my_assets,snapshot_dom)
from sbi_asset_runtime import read_current_view,select_asset_page as select_runtime


class CurrentAssetRecoveryBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:from playwright.sync_api import sync_playwright
        except ImportError:raise unittest.SkipTest('Playwright is not installed')
        cls.pw=sync_playwright().start()
        from browser_test_support import browser_executable
        executable=browser_executable(cls.pw)
        if executable is None:
            cls.pw.stop();raise unittest.SkipTest('Chromium is not installed')
        cls.browser=cls.pw.chromium.launch(executable_path=executable,headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context();self.page=self.context.new_page()
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.enterContext(patch.dict('family_profile.OWNERS',{'A':'架空太郎','B':'架空次郎'},clear=True))
    def tearDown(self):
        self.store.close();self.tmp.cleanup();self.context.close()
    def route(self,pages):
        self.context.route('**/*',lambda route:route.fulfill(body=pages.get(urlparse(route.request.url).path,''),content_type='text/html; charset=utf-8'))
    def post(self):
        self.page.goto('https://plus.e-shishobako.ne.jp/post')

    def test_post_entry_reuses_same_context_authenticated_sbi_tab(self):
        self.route({'/home':'<p>ログアウト 口座管理</p>','/post':'<p>電子ポスト</p>'})
        sbi=self.context.new_page();sbi.goto('https://site3.sbisec.co.jp/home');self.post()
        selected=select_asset_page(self.page,owner='架空太郎',identity_verified=True)
        self.assertIs(selected,sbi);self.assertEqual(len(self.context.pages),2)
        self.assertFalse(self.page.is_closed())

    def test_same_tab_post_redirect_restores_only_observed_entry_url(self):
        self.route({'/home':'<p>ログアウト 口座管理</p>','/post':'<p>電子ポスト</p>'});self.post()
        messages=[];entry='https://site3.sbisec.co.jp/home?opaque=synthetic-session'
        selected=select_asset_page(self.page,owner='架空太郎',identity_verified=True,entry_url=entry,progress=messages.append)
        self.assertEqual(selected.url,entry);self.assertIs(selected.context,self.context)
        self.assertEqual(len(self.context.pages),2);self.assertFalse(self.page.is_closed())
        self.assertNotIn('opaque',' '.join(messages));self.assertNotIn('synthetic-session',' '.join(messages))

    def test_stale_login_tab_does_not_block_observed_home_recovery(self):
        self.route({'/home':'<p>ログアウト 口座管理</p>','/login':'<input type="password">','/post':'<p>電子ポスト</p>'})
        stale=self.context.new_page();stale.goto('https://site3.sbisec.co.jp/login');self.post()
        selected=select_asset_page(self.page,owner='架空太郎',identity_verified=True,entry_url='https://site3.sbisec.co.jp/home')
        self.assertEqual(selected.url,'https://site3.sbisec.co.jp/home')
        self.assertFalse(stale.is_closed());self.assertFalse(self.page.is_closed())

    def test_untrusted_entry_is_never_opened(self):
        self.route({'/post':'電子ポスト'});self.post();requests=[]
        self.context.on('request',lambda request:requests.append(request.url))
        with self.assertRaises(CredentialError):
            select_asset_page(self.page,owner='架空太郎',identity_verified=True,entry_url='https://untrusted.example/home?session=private')
        self.assertEqual(requests,[]);self.assertEqual(len(self.context.pages),1)

    def test_wrong_owner_on_any_sbi_tab_is_fatal_even_with_post_proof(self):
        self.route({'/home':'<p>ログアウト 口座管理 架空次郎 様</p>','/post':'電子ポスト'})
        other=self.context.new_page();other.goto('https://site3.sbisec.co.jp/home');self.post()
        with self.assertRaises(CredentialError):select_asset_page(self.page,owner='架空太郎',identity_verified=True)
        self.assertEqual(len(self.context.pages),2)

    def test_password_form_is_not_an_authenticated_asset_candidate(self):
        self.route({'/login':'<p>ログアウト 口座管理</p><input type="password">'})
        self.page.goto('https://site3.sbisec.co.jp/login')
        with self.assertRaises(CredentialError):select_runtime(self.page,owner='架空太郎',identity_verified=True,timeout=.3)

    def test_equivalent_relative_absolute_links_beat_duplicate_js_caption(self):
        self.route({'/home':'''<a href="/assets">My資産</a><a href="https://site3.sbisec.co.jp/assets">My資産</a>
          <button onclick="document.body.dataset.wrong='yes'">My資産</button>''','/assets':'<h1>My資産</h1>'})
        self.page.goto('https://site3.sbisec.co.jp/home');selected=navigate(self.page,['My資産'])
        self.assertEqual(selected.url,'https://site3.sbisec.co.jp/assets')

    def test_truly_distinct_js_controls_are_not_guessed(self):
        self.route({'/home':'''<button onclick="document.body.dataset.action='one'">My資産</button>
          <button onclick="document.body.dataset.action='two'">My資産</button>'''})
        self.page.goto('https://site3.sbisec.co.jp/home')
        with self.assertRaises(CredentialError):navigate(self.page,['My資産'],timeout=.2)
        self.assertIsNone(self.page.locator('body').get_attribute('data-action'))

    def test_cash_filter_applies_nearby_refresh_and_awaits_async_amounts(self):
        self.route({'/assets':'''<h1>My資産</h1><section>
          <label><input type="checkbox" checked onchange="document.querySelector('button').disabled=false">預り金を除外</label>
          <button disabled onclick="const box=document.querySelector('#balances');box.setAttribute('aria-busy','true');setTimeout(()=>{box.innerHTML='<div>預り金(円)</div><div>700円</div>';box.setAttribute('aria-busy','false')},850)">更新</button>
          <div id="balances"><div>国内株式(現物)</div><div>100円</div></div></section>'''})
        self.page.goto('https://site3.sbisec.co.jp/assets');include_cash(self.page)
        text,tables,values=read_current_view(self.page,'My資産',timeout=3)
        self.assertEqual(values['cash'],700)
        self.assertNotIn('domestic',values)

    def test_legacy_summary_waits_for_nisa_blocks_after_cash_block(self):
        self.route({'/summary':'''<h1>口座サマリー</h1><div>現金残高等（合計）</div><div>3,400</div><div id="nisa"></div>
          <script>setTimeout(()=>document.querySelector('#nisa').innerHTML='<div>投資可能枠(2026年)－NISA(成長投資枠)</div><div>575</div><div>投資可能枠(2026年)－NISA(つみたて投資枠)</div><div>1,200,000</div>',1400)</script>'''})
        self.page.goto('https://site3.sbisec.co.jp/summary')
        _,_,values=read_current_view(self.page,'口座サマリー',timeout=3)
        self.assertEqual(values['growth_remaining']['remaining'],575)
        self.assertEqual(values['tsumitate_remaining']['remaining'],1200000)

    def test_optional_filter_ambiguity_preserves_summary_and_continues_currency(self):
        from sbi_views import _jst_now
        year=_jst_now().year
        nav='<a href="/summary">口座管理</a><a href="/summary">口座（円建）</a><a href="/assets">My資産</a><a href="/foreign">口座（外貨建）</a>'
        summary=f'''<h1>口座サマリー</h1><div>現金残高等（合計）</div><div>3,400</div>
          <div>投資可能枠({year}年)－NISA(成長投資枠)</div><div>575</div>
          <div>投資可能枠({year}年)－NISA(つみたて投資枠)</div><div>1,200,000</div>'''
        pages={'/home':'<p>ログアウト 口座管理 架空太郎 様</p>', '/summary':summary,
          '/assets':'<h1>My資産</h1><label><input type="checkbox" checked>預り金を除外</label><label><input type="checkbox" checked>預り金を除外</label>',
          '/foreign':'<h1>口座（外貨建）</h1><div>米ドル預り金</div><div>10.25 USD</div>'}
        self.context.route('**/*',lambda route:route.fulfill(body=nav+pages.get(urlparse(route.request.url).path,''),content_type='text/html; charset=utf-8'))
        self.page.goto('https://site3.sbisec.co.jp/home');messages=[]
        self.assertFalse(capture_account(self.page,self.store,'A',owner='架空太郎',progress=messages.append))
        recorded=latest(self.store,'2099-01-01')
        self.assertEqual(recorded['A','口座サマリー']['values']['growth_remaining']['remaining'],575)
        self.assertEqual(recorded['A','口座(外貨建)']['values']['usd_cash'],10.25)
        self.assertTrue(any('表示切替が複数' in message for message in messages))

    def test_screenshot_shaped_summary_and_two_react_rows_survive_epost_handoff(self):
        from sbi_views import _jst_now
        year=_jst_now().year
        nav='<a href="/summary">口座管理</a><a href="/summary">口座（円建）</a><a href="/assets">My資産</a><a href="https://site3.sbisec.co.jp/assets">My資産</a><a href="/foreign">口座（外貨建）</a>'
        summary=f'''<h1>口座サマリー</h1><table>
          <tr><td>現金残高等（合計）</td><td>3,400</td></tr><tr><td>SBI証券口座分</td><td>0</td></tr>
          <tr><td>スィープ専用銀行口座</td><td>3,400</td></tr><tr><td>株式</td><td>1,200</td></tr><tr><td>計</td><td>4,600</td></tr>
          <tr><td>投資可能枠({year}年)－NISA(成長投資枠)</td><td>575</td></tr>
          <tr><td>投資可能枠({year}年)－NISA(つみたて投資枠)</td><td>1,200,000</td></tr></table>'''
        assets='''<h1>My資産</h1><div>資産残高</div><section><label><input type="checkbox" checked>預り金を除外</label><button onclick="document.body.dataset.refreshed='yes'">更新</button></section>
          <div>2026/10/22 17:10</div><div>4,600円</div><div>資産構成比率</div>
          <div>国内株式(現物)</div><div>スィープ専用銀行口座</div><div>商品</div><div>評価額</div><div>評価損益</div><div>前日比/率(%)</div>
          <div>国内株式(現物)</div><div>1,200円</div><div>+100円</div><div>+9.09%</div>
          <div>スィープ専用銀行口座</div><div>3,400円</div><div>--</div><div>0円</div><div>0.00%</div><div>合計</div><div>4,600円</div>'''
        pages={'/home':'<p>ログアウト 口座管理</p>','/summary':summary,'/assets':assets,'/foreign':'<div>米ドル預り金</div><div>10.25 USD</div>','/post':'電子ポスト'}
        self.context.route('**/*',lambda route:route.fulfill(body=nav+pages.get(urlparse(route.request.url).path,''),content_type='text/html; charset=utf-8'))
        self.post();restored=select_asset_page(self.page,owner='架空太郎',identity_verified=True,entry_url='https://site3.sbisec.co.jp/home')
        self.assertFalse(capture_account(restored,self.store,'A',owner='架空太郎',identity_verified=True))
        records=latest(self.store,'2099-01-01');status=capture_status(self.store,'A')
        self.assertEqual(records['A','My資産']['values']['domestic'],1200)
        self.assertEqual(records['A','My資産']['values']['bank'],3400)
        self.assertEqual(records['A','口座サマリー']['values']['growth_remaining']['remaining'],575)
        self.assertEqual(records['A','口座サマリー']['values']['tsumitate_remaining']['remaining'],1200000)
        self.assertEqual(set(status['available']),{'cash','growth_remaining','tsumitate_remaining'})
        self.assertIn('foreign',status['missing']);self.assertIn('foreign_cash',status['missing'])
        self.assertNotIn('foreign',records['A','My資産']['values'])


if __name__=='__main__':unittest.main()
