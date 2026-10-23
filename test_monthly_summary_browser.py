"""Real Chromium against intercepted, anonymous legacy summary pages only."""
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from urllib.parse import urlparse

from ledger import Store
from login_credentials import CredentialError
from sbi_views import latest
from monthly_summary import capture_summary,_read_values


class MonthlySummaryBrowser(unittest.TestCase):
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
    def summary(self,rows):
        return '<p>ログアウト 口座サマリー</p><table><tr><td>| 保有資産評価<br>詳細</td></tr>'+rows+'</table>'
    def open_summary(self,html):
        self.route({'/summary':html});self.page.goto('https://site3.sbisec.co.jp/summary')

    def test_summary_only_navigation_captures_stocks_cash_without_other_workflows(self):
        summary=self.summary('''<tr><td>現金残高等（合計）</td><td>3,400</td></tr>
          <tr><td>SBI証券口座分</td><td>0</td></tr><tr><td>スィープ専用銀行口座</td><td>3,400</td></tr>
          <tr><td>株式</td><td>1,200</td></tr><tr><td>計</td><td>4,600</td></tr>''')+'''
          <table><tr><td>保有資産評価</td><td>詳細</td></tr></table>
          <h2>ポートフォリオ</h2><table><tr><td>株式</td><td>999,999</td></tr><tr><td>計</td><td>999,999</td></tr></table>
          <p>2026/10/23 15:57:00</p>'''
        home='''<p>ログアウト 口座管理 架空 太郎 様</p><a href="/summary">口座管理</a>
          <a href="/myassets">My資産</a><a href="/foreign">口座（外貨建）</a><a href="/epost">電子ポスト</a>'''
        self.route({'/home':home,'/summary':summary});self.page.goto('https://site3.sbisec.co.jp/home')
        requests=[];self.context.on('request',lambda request:requests.append(urlparse(request.url).path))
        now=datetime(2026,10,23,16,0,12,tzinfo=timezone(timedelta(hours=9)))
        with patch('sbi_views._jst_now',return_value=now),patch('epost_collector.collect') as pdf,patch('sbi_views.capture_account') as full:
            result=capture_summary(self.page,self.store,'A',owner='架空太郎')
        self.assertTrue(result['complete']);self.assertEqual(result['missing'],[])
        self.assertEqual(result['values']['summary_stocks'],1200)
        self.assertEqual(result['values']['cash_total'],3400)
        self.assertEqual(result['observed_at'],'2026-10-23T16:00:12+09:00')
        self.assertEqual(result['displayed_at'],'2026/10/23 15:57:00')
        self.assertEqual(set(requests),{'/summary'});pdf.assert_not_called();full.assert_not_called()
        observed=latest(self.store,'2099-01-01')['A','口座サマリー']
        self.assertEqual(observed['values']['summary_total'],4600)
        self.assertNotIn('domestic',result['values'])

    def test_hidden_old_rows_are_excluded_from_scoped_values(self):
        self.open_summary(self.summary('''<tr><td>現金残高等(合計)</td><td>200</td></tr>
          <tr style="display:none"><td>株式</td><td>999</td></tr><tr><td>株式</td><td>100</td></tr>
          <tr><td>計</td><td>300</td></tr>'''))
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertTrue(result['complete']);self.assertEqual(result['values']['summary_stocks'],100)

    def test_collapsed_old_table_row_does_not_conflict_with_visible_balance(self):
        self.open_summary(self.summary('''<tr><td>現金残高等(合計)</td><td>200</td></tr>
          <tr style="visibility:collapse"><td>株式</td><td>999</td></tr><tr><td>株式</td><td>100</td></tr>
          <tr><td>計</td><td>300</td></tr>'''))
        self.assertEqual(_read_values(self.page)[1]['summary_stocks'],100)

    def test_header_only_table_cannot_scope_unheaded_sibling_portfolio_table(self):
        self.open_summary('''<p>ログアウト 口座サマリー</p><table><tr><td>保有資産評価</td><td>詳細</td></tr></table>
          <table><tr><td>株式</td><td>999</td></tr><tr><td>現金残高等(合計)</td><td>100</td></tr><tr><td>計</td><td>1099</td></tr></table>''')
        self.assertEqual(_read_values(self.page)[1],{})

    def test_hidden_trusted_frame_does_not_override_visible_summary(self):
        html=self.summary('<tr><td>現金残高等(合計)</td><td>200</td></tr><tr><td>株式</td><td>100</td></tr>')+'<iframe style="display:none" src="/old"></iframe>'
        old=self.summary('<tr><td>現金残高等(合計)</td><td>999</td></tr><tr><td>株式</td><td>999</td></tr>')
        self.route({'/summary':html,'/old':old});self.page.goto('https://site3.sbisec.co.jp/summary')
        self.assertEqual(_read_values(self.page)[1],{'cash_total':200,'summary_stocks':100})

    def test_hyphen_page_clock_is_preserved_separately_from_acquisition_time(self):
        self.open_summary(self.summary('<tr><td>現金残高等(合計)</td><td>200</td></tr><tr><td>株式</td><td>100</td></tr>')+'<p>2026-10-23 15:57:00</p>')
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertEqual(result['displayed_at'],'2026/10/23 15:57:00')

    def test_zero_stocks_and_cash_are_complete_without_optional_nisa(self):
        self.open_summary(self.summary('<tr><td>現金残高等(合計)</td><td>0</td></tr><tr><td>株式</td><td>0</td></tr><tr><td>計</td><td>0</td></tr>'))
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertTrue(result['complete']);self.assertEqual(result['values']['summary_stocks'],0)
        self.assertEqual(result['values']['cash_total'],0)
        self.assertNotIn('growth_remaining',result['values'])

    def test_missing_stock_is_partial_and_never_filled_from_residual(self):
        self.open_summary(self.summary('<tr><td>現金残高等(合計)</td><td>200</td></tr><tr><td>株式</td><td>--</td></tr><tr><td>計</td><td>200</td></tr>'))
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertFalse(result['complete']);self.assertEqual(result['missing'],['summary_stocks'])
        self.assertNotIn('summary_stocks',result['values']);self.assertEqual(result['values']['cash_total'],200)
        self.assertEqual(latest(self.store,'2099-01-01')['A','口座サマリー']['values']['cash_total'],200)

    def test_cash_block_waits_for_delayed_stock_block(self):
        html=self.summary('<tr><td>現金残高等(合計)</td><td>200</td></tr>')+'''
          <script>setTimeout(()=>document.querySelector('table').insertAdjacentHTML('beforeend','<tr><td>株式</td><td>100</td></tr><tr><td>計</td><td>300</td></tr>'),1200)</script>'''
        self.open_summary(html)
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertTrue(result['complete']);self.assertEqual(result['values']['summary_stocks'],100)

    def test_loading_summary_is_not_saved_until_balances_update(self):
        html=self.summary('<tr><td>現金残高等(合計)</td><td>0</td></tr><tr><td>株式</td><td>0</td></tr><tr><td>計</td><td>0</td></tr>')+'''
          <script>const table=document.querySelector('table');table.setAttribute('aria-busy','true');setTimeout(()=>{
          table.innerHTML='<tr><td>保有資産評価</td></tr><tr><td>現金残高等(合計)</td><td>200</td></tr><tr><td>株式</td><td>100</td></tr><tr><td>計</td><td>300</td></tr>';table.setAttribute('aria-busy','false')},900)</script>'''
        self.open_summary(html)
        result=capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True)
        self.assertEqual(result['values']['summary_stocks'],100)
        self.assertEqual(result['values']['cash_total'],200)

    def test_react_myassets_categories_are_not_misread_as_legacy_summary(self):
        self.open_summary('<p>ログアウト My資産</p><p>国内株式(現物)</p><p>1,200円</p><p>スィープ専用銀行口座</p><p>3,400円</p>')
        text,values=_read_values(self.page)
        self.assertEqual(values,{})

    def test_wrong_owner_after_navigation_stops_before_observation_write(self):
        home='<p>ログアウト 口座管理 架空太郎 様</p><a href="/summary">口座管理</a>'
        summary=self.summary('<tr><td>株式</td><td>100</td></tr><tr><td>現金残高等(合計)</td><td>200</td></tr>')+'<p>架空次郎 様</p>'
        self.route({'/home':home,'/summary':summary});self.page.goto('https://site3.sbisec.co.jp/home')
        with self.assertRaises(CredentialError):capture_summary(self.page,self.store,'A',owner='架空太郎')
        self.assertEqual(latest(self.store,'2099-01-01'),{})

    def test_cancelled_measurement_never_writes(self):
        self.open_summary(self.summary('<tr><td>株式</td><td>100</td></tr><tr><td>現金残高等(合計)</td><td>200</td></tr>'))
        with self.assertRaises(CredentialError):
            capture_summary(self.page,self.store,'A',owner='架空太郎',identity_verified=True,cancelled=lambda:True)
        self.assertEqual(latest(self.store,'2099-01-01'),{})


if __name__=='__main__':unittest.main()
