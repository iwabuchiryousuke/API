"""Exact-date history regression tests. No live SBI credentials or network."""
import json
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import Mock,patch

from ledger import Store,LedgerError
from asset_snapshots import (source_date,displayed_asset_date,exact_table_values,
    exact_panel_values,merge_exact,historical_status,save,snapshots,SNAPSHOT_FIELDS,HistoricalConflict)
from sbi_views import capture_account,_jst_now,parse_summary,capture_exact_dates

HEADERS=['日付','国内株式等(円)','外国株式(円)','現金合計(円)','外貨現金(円)',
         '成長枠残高(円)','積立枠残高(円)']


class HistoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.enterContext(patch('sbi_views.select_asset_page',side_effect=lambda page,**_:page))
        def immediate(page,kind,**kwargs):
            from sbi_views import snapshot_dom,parse_summary,parse_my_assets,parse_currency_summary
            text,tables=snapshot_dom(page)
            parser={'口座サマリー':parse_summary,'My資産':parse_my_assets,'口座(外貨建)':parse_currency_summary}[kind]
            return text,tables,parser(text,tables)
        self.enterContext(patch('sbi_asset_runtime.read_current_view',side_effect=immediate))
    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_dates_include_calendar_weekends_and_leap_days(self):
        self.assertEqual(source_date('2026/02/28(土)'),'2026-02-28')
        self.assertEqual(source_date('2024年2月29日'),'2024-02-29')
        with self.assertRaises(LedgerError):source_date('2026/02/29')

    def test_exact_history_table_includes_dated_nisa_and_real_zero(self):
        rows=[HEADERS,['2026/09/30','100','200','300','0','0','1,200,000']]
        values=exact_table_values([rows],'2026-09-30')
        self.assertEqual(len(values),6);self.assertEqual(values['foreign_cash'],0)
        self.assertEqual(values['growth_remaining'],0)

    def test_other_date_or_currency_does_not_fill_target(self):
        rows=[HEADERS,['2026/09/29','100','200','300','400','500','600'],
              ['2026/09/30','100 USD','--','','0','0','1,200,000']]
        values=exact_table_values([rows],'2026-09-30')
        self.assertNotIn('domestic',values);self.assertNotIn('foreign',values)
        self.assertNotIn('cash',values);self.assertEqual(values['foreign_cash'],0)

    def test_duplicate_conflicting_rows_stop(self):
        rows=[HEADERS,['2026/09/30','100','200','300','400','500','600'],
              ['2026/09/30','101','200','300','400','500','600']]
        with self.assertRaises(LedgerError):exact_table_values([rows],'2026-09-30')

    def test_explicit_date_and_timestamp_must_agree(self):
        with self.assertRaises(LedgerError):
            displayed_asset_date('評価基準日2026/09/30\n2026/10/21 12:00')
        self.assertEqual(displayed_asset_date('評価基準日2026/09/30'),'2026-09-30')

    def test_requested_date_never_changes_stale_source(self):
        with self.assertRaises(LedgerError):
            exact_panel_values('評価基準日2026/10/21\n国内株式等(円)\n100',[],'2026-09-30')

    def test_whole_page_cannot_date_bind_current_nisa(self):
        text='基準日2026/09/30\n国内株式等(円)\n100\n成長枠残高(円)\n500\n投資可能枠(2026年)－NISA(つみたて投資枠)\n600'
        values=exact_panel_values(text,[],'2026-09-30',allow_nisa=False)
        self.assertEqual(values,{'domestic':100})
        bound=exact_panel_values(text,[],'2026-09-30',allow_nisa=True)
        self.assertEqual(bound['growth_remaining'],500)
        self.assertEqual(bound['tsumitate_remaining'],600)

    def test_nisa_year_mismatch_stays_unknown(self):
        text='基準日2025/09/30\n国内株式等(円)\n100\n投資可能枠(2026年)－NISA(成長投資枠)\n500'
        self.assertNotIn('growth_remaining',exact_panel_values(text,[],'2025-09-30',allow_nisa=True))

    def test_partial_exact_merges_without_clearing_and_is_idempotent(self):
        self.assertTrue(merge_exact(self.store,'A','2026-09-30',{'domestic':100,'growth_remaining':0}))
        self.assertTrue(merge_exact(self.store,'A','2026-09-30',{'cash':300,'tsumitate_remaining':600}))
        row=snapshots(self.store)['A','2026-09-30']
        self.assertEqual(row['domestic'],100);self.assertEqual(row['growth_remaining'],0)
        self.assertEqual(row['cash'],300);self.assertEqual(row['tsumitate_remaining'],600)
        self.assertFalse(merge_exact(self.store,'A','2026-09-30',{'domestic':100}))
        with self.assertRaises(LedgerError):merge_exact(self.store,'A','2026-09-30',{'domestic':101})
        self.assertEqual(snapshots(self.store)['A','2026-09-30']['domestic'],100)

    def test_manual_row_is_preserved_including_its_blanks(self):
        save(self.store,'A','2026-09-30',{'domestic':999})
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        with self.assertRaises(HistoricalConflict):merge_exact(self.store,'A','2026-09-30',{'domestic':100,'cash':300})
        self.assertFalse(merge_exact(self.store,'A','2026-09-30',{'cash':300}))
        after=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        self.assertEqual(before,after)

    def test_derived_first_day_is_not_exact_and_exact_can_supersede_it(self):
        save(self.store,'A','2026-10-01',{'domestic':100,'cash':200},'SBI My資産の前月比','1日07:00')
        self.assertEqual(historical_status(self.store,'A',['2026-10-01'])['2026-10-01']['available'],[])
        merge_exact(self.store,'A','2026-10-01',{'domestic':101})
        row=snapshots(self.store)['A','2026-10-01']
        self.assertEqual(row['domestic'],101);self.assertIsNone(row['cash'])
        self.assertEqual(row['evidence']['method'],'displayed-date')

    def test_acquisition_day_current_observation_is_not_historical_evidence(self):
        save(self.store,'A','2026-10-01',{key:100 for key in SNAPSHOT_FIELDS},'SBI取得当日')
        status=historical_status(self.store,'A',['2026-10-01'])['2026-10-01']
        self.assertFalse(status['complete']);self.assertEqual(len(status['missing']),6)

    def test_current_capture_preserves_same_day_exact_metadata(self):
        day=_jst_now().date().isoformat()
        merge_exact(self.store,'A',day,{'domestic':999,'growth_remaining':0})
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        page=Mock();page.url='https://site.sbisec.co.jp/account/assets'
        text='現金残高等(合計)\n100\n国内株式(現物)\n200円'
        with patch('sbi_views.navigate',return_value=page),patch('sbi_views.snapshot_dom',return_value=(text,[])),patch('sbi_views.include_cash'),patch('sbi_views.select_monthly_view'):
            capture_account(page,self.store,'A',identity_verified=True)
        self.assertEqual(before,self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0])

    def test_responsive_nisa_horizontal_remaining_column(self):
        table=[['枠','成長投資枠','つみたて投資枠'],['年間投資枠(円)','2,400,000','1,200,000'],['投資可能額(円)','0','800,000']]
        value=parse_summary('NISA投資可能枠(2026年)',[table])
        self.assertEqual(value['growth_remaining'],{'year':2026,'remaining':0})
        self.assertEqual(value['tsumitate_remaining']['remaining'],800000)

    def test_responsive_nisa_vertical_requires_year_and_remaining_header(self):
        table=[['枠','年間投資枠(円)','投資可能額(円)'],['成長投資枠','2,400,000','100,000'],['つみたて投資枠','1,200,000','800,000']]
        value=parse_summary('投資可能枠(2026年)',[table])
        self.assertEqual(value['growth_remaining']['remaining'],100000)
        with self.assertRaises(LedgerError):parse_summary('NISA',[table])
        with self.assertRaises(LedgerError):parse_summary('投資可能枠(2026年)',[[['枠','年間投資枠(円)'],['成長投資枠','2,400,000']]])

    def test_new_display_conflict_cannot_reconfirm_complete_cached_history(self):
        values={key:100 for key in SNAPSHOT_FIELDS}
        merge_exact(self.store,'A','2026-09-30',values)
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        page=Mock();page.url='https://site.sbisec.co.jp/account/history';page.frames=[page]
        page.locator.return_value.evaluate_all.return_value=[]
        table=[HEADERS,['2026/09/30','101','100','100','100','100','100']]
        messages=[]
        with patch('sbi_views.snapshot_dom',return_value=('資産履歴',[table])),patch('sbi_views._history_busy',return_value=False),patch('sbi_views.navigate') as nav:
            with self.assertRaises(HistoricalConflict):
                capture_exact_dates(page,self.store,'A',('2026-09-30',),progress=messages.append)
        nav.assert_not_called()
        self.assertEqual(before,self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0])
        self.assertFalse(any('確認しました' in message for message in messages))

    def test_matching_display_reconfirms_cached_history_without_rewrite(self):
        merge_exact(self.store,'A','2026-09-30',{key:100 for key in SNAPSHOT_FIELDS})
        before=self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0]
        page=Mock();page.url='https://site.sbisec.co.jp/account/history';page.frames=[page]
        page.locator.return_value.evaluate_all.return_value=[]
        table=[HEADERS,['2026/09/30','100','100','100','100','100','100']]
        with patch('sbi_views.snapshot_dom',return_value=('資産履歴',[table])),patch('sbi_views._history_busy',return_value=False),patch('sbi_views.navigate') as nav:
            result=capture_exact_dates(page,self.store,'A',('2026-09-30',))
        self.assertTrue(result['2026-09-30']['complete']);nav.assert_not_called()
        self.assertEqual(before,self.store.db.execute('SELECT payload FROM asset_snapshots').fetchone()[0])

    def test_ambiguous_displayed_rows_cannot_reconfirm_cached_history(self):
        merge_exact(self.store,'A','2026-09-30',{key:100 for key in SNAPSHOT_FIELDS})
        page=Mock();page.url='https://site.sbisec.co.jp/account/history';page.frames=[page]
        table=[HEADERS,['2026/09/30','100','100','100','100','100','100'],['2026/09/30','101','100','100','100','100','100']]
        with patch('sbi_views.snapshot_dom',return_value=('資産履歴',[table])),patch('sbi_views._history_busy',return_value=False):
            with self.assertRaises(HistoricalConflict):capture_exact_dates(page,self.store,'A',('2026-09-30',))

    def test_targeted_capture_never_uses_monthly_delta_baseline(self):
        page=Mock();page.url='https://site.sbisec.co.jp/account/assets'
        text='現金残高等(合計)\n100\n国内株式(現物)\n200円'
        with patch('sbi_views.navigate',return_value=page),patch('sbi_views.snapshot_dom',return_value=(text,[])),patch('sbi_views.include_cash'),patch('asset_snapshots.capture_month_start') as derived,patch('sbi_views.capture_exact_dates',return_value={'2026-09-30':{'complete':False}}):
            complete=capture_account(page,self.store,'A',identity_verified=True,target_dates=('2026-09-30',))
        derived.assert_not_called();self.assertFalse(complete)


if __name__=='__main__':unittest.main()
