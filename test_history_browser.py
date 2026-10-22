"""Real Chromium, fully intercepted synthetic history controls and dates."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ledger import Store,LedgerError
from login_credentials import CredentialError
from asset_snapshots import snapshots,historical_status,merge_exact,SNAPSHOT_FIELDS,HistoricalConflict
from sbi_views import capture_exact_dates,_history_values,reveal_asset_rows,snapshot_dom,parse_my_assets,_set_history_date


class HistoricalBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:from playwright.sync_api import sync_playwright
        except ImportError:raise unittest.SkipTest('Playwright is not installed')
        cls.pw=sync_playwright().start()
        executable=shutil.which('chromium') or shutil.which('chromium-browser')
        if executable is None and not Path(cls.pw.chromium.executable_path).exists():
            cls.pw.stop();raise unittest.SkipTest('Chromium is not installed')
        cls.browser=cls.pw.chromium.launch(executable_path=executable,headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context();self.page=self.context.new_page()
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):
        self.store.close();self.tmp.cleanup();self.context.close()
    def html(self,html):
        self.context.route('**/*',lambda route:route.fulfill(body=html,content_type='text/html; charset=utf-8'))
        self.page.goto('https://site.sbisec.co.jp/account/history')

    def fixture(self,delayed=False,stale=False):
        self.html('''<h1>My資産 資産履歴</h1>
        <form onsubmit="return false"><label>指定日<input type="date" value="2026-10-21"></label>
        <button type="button" onclick="load()">表示</button></form>
        <p id="requested"></p>
        <section id="result" aria-label="指定日資産" aria-busy="false"><h2>基準日2026/10/21</h2><div>国内株式等(円)</div><div>999円</div></section>
        <script>
        function contents(day) {
          let n=Number(day.slice(-2));
          let labels=['国内株式等(円)','外国株式(円)','現金合計(円)','外貨現金(円)'];
          let html='<h2>基準日'+day.replaceAll('-','/')+'</h2>';
          labels.forEach((label,i)=>html+='<div>'+label+'</div><div>'+(1000+i*100+n)+'円</div>');
          html+='<div>投資可能枠('+day.slice(0,4)+'年)－NISA(成長投資枠)</div><div>0円</div>';
          html+='<div>投資可能枠('+day.slice(0,4)+'年)－NISA(つみたて投資枠)</div><div>1,200,000円</div>';
          return html;
        }
        function load() {
          let day=document.querySelector('input').value;
          document.querySelector('#requested').textContent=day;
          if(STALE)return;
          let result=document.querySelector('#result');
          result.setAttribute('aria-busy','true');
          result.querySelector('h2').textContent='基準日'+day.replaceAll('-','/');
          setTimeout(()=>{result.innerHTML=contents(day);result.setAttribute('aria-busy','false')},DELAY);
        }
        </script>'''.replace('STALE','true' if stale else 'false').replace('DELAY','900' if delayed else '0'))

    def test_two_real_date_queries_fill_exact_calendar_boundaries_and_nisa(self):
        self.fixture()
        result=capture_exact_dates(self.page,self.store,'A',('2026-09-30','2026-10-01'),timeout=3)
        self.assertTrue(result['2026-09-30']['complete']);self.assertTrue(result['2026-10-01']['complete'])
        rows=snapshots(self.store)
        self.assertEqual(rows['A','2026-09-30']['domestic'],1030)
        self.assertEqual(rows['A','2026-10-01']['domestic'],1001)
        self.assertEqual(rows['A','2026-09-30']['growth_remaining'],0)
        self.assertEqual(rows['A','2026-10-01']['evidence']['as_of'],'2026-10-01')

    def test_weekend_month_end_is_not_shifted_to_last_trading_day(self):
        self.fixture()
        result=capture_exact_dates(self.page,self.store,'A',('2026-02-28','2026-03-01'),timeout=3)
        self.assertTrue(all(value['complete'] for value in result.values()))
        self.assertIn(('A','2026-02-28'),snapshots(self.store))
        self.assertNotIn(('A','2026-02-27'),snapshots(self.store))

    def test_heading_changes_before_amounts_waits_for_loaded_panel(self):
        self.fixture(delayed=True)
        result=capture_exact_dates(self.page,self.store,'A',('2026-09-30',),timeout=3)
        self.assertTrue(result['2026-09-30']['complete'])
        self.assertEqual(snapshots(self.store)['A','2026-09-30']['domestic'],1030)

    def test_date_input_value_alone_is_not_displayed_date_proof(self):
        self.fixture(stale=True)
        result=capture_exact_dates(self.page,self.store,'A',('2026-09-30',),timeout=1.2)
        self.assertFalse(result['2026-09-30']['complete'])
        self.assertNotIn(('A','2026-09-30'),snapshots(self.store))
        self.assertEqual(self.page.locator('input').input_value(),'2026-09-30')

    def test_current_nisa_sibling_is_not_merged_into_dated_history(self):
        self.html('''<section aria-label="指定日資産"><h1>資産履歴</h1>
        <aside>投資可能枠(2026年)－NISA(成長投資枠)<br>999,999円</aside>
        <section aria-label="指定日資産"><h2>基準日2026/09/30</h2>
        <div>国内株式等(円)</div><div>100円</div></section></section>''')
        values=_history_values(self.page,'2026-09-30')
        self.assertEqual(values,{'domestic':100})

    def test_dated_table_is_read_without_date_control_guessing(self):
        headers=['日付','国内株式等(円)','外国株式(円)','現金合計(円)','外貨現金(円)','成長枠残高(円)','積立枠残高(円)']
        row=['2026/09/30','100','200','300','0','0','1,200,000']
        self.html('<h1>資産履歴</h1><table><tr>'+''.join('<th>'+x+'</th>' for x in headers)+'</tr><tr>'+''.join('<td>'+x+'</td>' for x in row)+'</tr></table>')
        with patch('sbi_views.navigate') as nav:
            result=capture_exact_dates(self.page,self.store,'A',('2026-09-30',),timeout=2)
        nav.assert_not_called();self.assertTrue(result['2026-09-30']['complete'])

    def test_cancel_after_query_does_not_save_new_snapshot(self):
        self.fixture(delayed=True)
        with self.assertRaises(CredentialError):
            capture_exact_dates(self.page,self.store,'A',('2026-09-30',),cancelled=lambda:self.page.locator('#requested').inner_text()=='2026-09-30',timeout=3)
        self.assertNotIn(('A','2026-09-30'),snapshots(self.store))

    def test_observed_range_fields_query_only_one_requested_day(self):
        self.html('''<h1>資産履歴</h1><form><label>開始日<input type="date" value="2026-10-01"></label>
        <label>終了日<input type="date" value="2026-10-21"></label>
        <button type="button" onclick="this.dataset.queried='yes'">照会</button></form>''')
        _set_history_date(self.page,'2026-09-30')
        self.assertEqual(self.page.get_by_label('開始日').input_value(),'2026-09-30')
        self.assertEqual(self.page.get_by_label('終了日').input_value(),'2026-09-30')
        self.assertEqual(self.page.get_by_role('button',name='照会').get_attribute('data-queried'),'yes')

    def test_observed_year_month_day_selects_use_matching_options(self):
        self.html('''<h1>My資産 資産履歴</h1><form>
        <label>年<select><option>2025年</option><option>2026年</option></select></label>
        <label>月<select><option>9月</option><option>10月</option></select></label>
        <label>日<select><option>1日</option><option>30日</option></select></label>
        <button type="button">表示</button></form>''')
        _set_history_date(self.page,'2026-09-30')
        self.assertEqual(self.page.locator('select').evaluate_all('els=>els.map(e=>e.value)'),['2026年','9月','30日'])

    def test_date_controls_across_different_forms_are_not_combined(self):
        self.html('''<h1>資産履歴</h1><form><label>開始日<input type="date" value="2026-10-01"></label></form>
        <form><label>終了日<input type="date" value="2026-10-21"></label></form>''')
        with self.assertRaises(LedgerError):_set_history_date(self.page,'2026-09-30')
        self.assertEqual(self.page.get_by_label('開始日').input_value(),'2026-10-01')

    def test_date_filter_outside_asset_context_is_not_operated(self):
        self.html('''<h1>取引ページ</h1><label>指定日<input type="date" value="2026-10-21"></label>''')
        with self.assertRaises(LedgerError):_set_history_date(self.page,'2026-09-30')
        self.assertEqual(self.page.get_by_label('指定日').input_value(),'2026-10-21')

    def test_rendered_conflict_with_complete_cached_history_stops(self):
        merge_exact(self.store,'A','2026-09-30',{key:100 for key in SNAPSHOT_FIELDS})
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        headers=['日付','国内株式等(円)','外国株式(円)','現金合計(円)','外貨現金(円)','成長枠残高(円)','積立枠残高(円)']
        row=['2026/09/30','101','100','100','100','100','100']
        self.html('<h1>資産履歴</h1><table><tr>'+''.join('<th>'+x+'</th>' for x in headers)+'</tr><tr>'+''.join('<td>'+x+'</td>' for x in row)+'</tr></table>')
        with self.assertRaises(HistoricalConflict):capture_exact_dates(self.page,self.store,'A',('2026-09-30',),timeout=2)
        self.assertEqual(before,self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0])

    def test_conflict_after_real_date_query_is_not_downgraded(self):
        merge_exact(self.store,'A','2026-09-30',{'domestic':100})
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        self.fixture()
        with self.assertRaises(HistoricalConflict):capture_exact_dates(self.page,self.store,'A',('2026-09-30',),timeout=3)
        self.assertEqual(self.page.locator('input').input_value(),'2026-09-30')
        self.assertEqual(before,self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0])

    def test_observed_zero_visibility_checkbox_reveals_explicit_zero(self):
        self.html('''<h1>My資産</h1><label><input type="checkbox" onchange="document.querySelector('#zero').hidden=!this.checked">残高ゼロを表示</label>
        <div>国内株式(現物)</div><div>100円</div><div id="zero" hidden><div>外国株式(米国)</div><div>0円</div><div>預り金(米ドル)</div><div>0 USD</div></div>''')
        before=parse_my_assets(*snapshot_dom(self.page));self.assertNotIn('foreign',before)
        reveal_asset_rows(self.page)
        after=parse_my_assets(*snapshot_dom(self.page))
        self.assertEqual(after['foreign'],0);self.assertEqual(after['usd_cash'],0)
        self.assertEqual(after['foreign_cash'],0)


if __name__=='__main__':unittest.main()
