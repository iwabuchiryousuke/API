"""Current-only cash/NISA reader boundaries with anonymous observations."""
from datetime import datetime, timezone, timedelta
import tempfile
import unittest
from unittest.mock import Mock, patch

from current_asset_reader import capture_current, _values
from auto_login import AcquisitionError, IdentityUnavailable
from ledger import Store
from login_credentials import CredentialError
from sbi_views import ObservationConflict
import test_monthly_login_modes as login_fixture

NOW=datetime(2026,10,29,14,5,tzinfo=timezone(timedelta(hours=9)))


class CurrentAssetValueTests(unittest.TestCase):
    def read(self, chunks, kind='口座サマリー'):
        with patch('current_asset_reader._snapshot',return_value=chunks),patch('sbi_views._jst_now',return_value=NOW):
            return _values(Mock(),kind)[1]

    def test_summary_reads_cash_and_explicit_current_year_nisa_without_stocks(self):
        text='''現金残高等（合計） 4,000円
SBI証券口座分 1,000円
スィープ専用銀行口座 3,000円
投資可能枠（2026年）－NISA（成長投資枠） 0円
投資可能枠（2026年）－NISA（つみたて投資枠） 1,200,000円'''
        value=self.read([(text,[])])
        self.assertEqual(value['cash_total'],4000)
        self.assertEqual(value['growth_remaining'],{'year':2026,'remaining':0})
        self.assertEqual(value['tsumitate_remaining']['remaining'],1200000)
        self.assertNotIn('summary_stocks',value)

    def test_old_selected_nisa_year_is_not_current_allowance(self):
        value=self.read([('現金残高等合計 2,000円\n投資可能枠(2025年)－NISA(成長投資枠) 999円',[])])
        self.assertEqual(value,{'cash_total':2000})

    def test_explicit_native_usd_is_not_yen_valuation_or_buying_power(self):
        table=[['通貨','買付余力','預り金','円換算額'],['米ドル','999','12.25','1,800']]
        value=self.read([('',[table])],'口座(外貨建)')
        self.assertEqual(value,{'usd_cash':12.25})
        self.assertEqual(self.read([('米ドル買付余力 999 USD\n外貨現金(円) 1,800円',[])],'口座(外貨建)'),{})

    def test_explicit_zero_usd_is_recorded_and_unseen_usd_is_not_zero(self):
        self.assertEqual(self.read([('米ドル残高 0 USD',[])],'口座(外貨建)'),{'usd_cash':0})
        self.assertEqual(self.read([('円換算額 0円',[])],'口座(外貨建)'),{})

    def test_responsive_cash_headers_require_an_explicit_usd_currency_row(self):
        for header in ('現金残高','外貨現金残高','外貨預り金残高','預り金(外貨)'):
            with self.subTest(header=header):
                self.assertEqual(self.read([('',[[['通貨',header],['米ドル','12.25']]])],'口座(外貨建)'),{'usd_cash':12.25})
                self.assertEqual(self.read([('',[[['通貨',header],['ユーロ','12.25']]])],'口座(外貨建)'),{})
        self.assertEqual(self.read([('米ドル現金残高 12.25 USD',[])],'口座(外貨建)'),{'usd_cash':12.25})

    def test_separate_frames_cannot_join_a_label_and_unrelated_number(self):
        self.assertEqual(self.read([('現金残高等合計',[]),('9,999円',[])]),{})

    def test_conflicting_cash_components_across_frames_are_rejected(self):
        with self.assertRaises(ObservationConflict):
            self.read([('現金残高等合計 4,000円',[]),('SBI証券口座分 1,000円\nスィープ専用銀行口座 3,001円',[])])

    def test_capture_only_creates_current_observations_and_reports_missing_usd(self):
        from sbi_views import latest
        page=Mock();page.frames=[];summary={'cash_total':100,'growth_remaining':{'year':2026,'remaining':0},
                             'tsumitate_remaining':{'year':2026,'remaining':1200000}}
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            try:
                with patch('sbi_views.select_asset_page',return_value=page),patch('sbi_views._check_asset_identity'), \
                     patch('current_asset_reader._show_view',side_effect=lambda page,*args:page), \
                     patch('current_asset_reader.read_current',side_effect=[('2026/10/29 14:05',summary),('',{})]), \
                     patch('sbi_views._jst_now',return_value=NOW),patch('sbi_views.capture_account') as full:
                    result=capture_current(page,store,'A',owner='Anonymous A',identity_verified=True)
                self.assertFalse(result['complete']);self.assertEqual(result['missing'],['usd_cash'])
                self.assertEqual(result['values']['cash_total'],100)
                self.assertNotIn('usd_cash',result['values'])
                values=latest(store,'2099-01-01')
                self.assertEqual(set(values),{('A','口座サマリー')})
                self.assertEqual(values['A','口座サマリー']['observed_at'],NOW.isoformat())
                self.assertFalse(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_snapshots'").fetchone())
                full.assert_not_called()
            finally:store.close()


class CurrentOnlyLoginTests(unittest.TestCase):
    def setUp(self):
        self.fixture=login_fixture.MonthlyLoginModes()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.status={'complete':True,'available':['cash_total','usd_cash','growth_remaining','tsumitate_remaining'],
                     'missing':[],'usd_cash_observed':True,'values':{'cash_total':100,'usd_cash':12.5},
                     'observed_at':NOW.isoformat(),'component_observed_at':{}}
        self.reader=Mock(return_value=self.status)
        self.fixture.mocks['current_asset_reader.capture_current']=self.reader

    def execute(self,**options):return self.fixture.execute(current_only=True,**options)

    def test_current_mode_preserves_pdf_flow_and_never_calls_full_asset_or_history_path(self):
        result=self.execute()
        self.assertTrue(result['complete']);self.assertTrue(result['pdf_collected'])
        self.assertTrue(result['assets_observed']);self.assertEqual(result['assets_missing'],[])
        self.assertIs(result['current_status'],self.status)
        self.fixture.mocks['auto_login.collect'].assert_called_once()
        self.fixture.mocks['sbi_views.capture_account'].assert_not_called()
        self.fixture.mocks['monthly_summary.capture_summary'].assert_not_called()
        self.assertEqual(self.fixture.mocks['acquisition_journal.persist_capture'].call_args.kwargs['target_dates'],())
        self.assertTrue(self.reader.call_args.kwargs['identity_verified'])

    def test_current_mode_rejects_historical_dates_before_starting_browser(self):
        with self.assertRaises(CredentialError):self.execute(target_dates=('2026-09-30',))
        self.fixture.pw.chromium.launch.assert_not_called();self.reader.assert_not_called()

    def test_current_mode_rejects_other_incompatible_acquisition_modes(self):
        for options in ({'summary_only':True,'documents':False},{'capture_assets':False}):
            with self.subTest(options=options),self.assertRaises(CredentialError):self.execute(**options)
        self.fixture.pw.chromium.launch.assert_not_called()

    def test_partial_usd_warns_once_and_keeps_verified_pdf_collection(self):
        self.status.update(complete=False,missing=['usd_cash'],usd_cash_observed=False,
                           available=['cash_total','growth_remaining','tsumitate_remaining'])
        result=self.execute()
        self.assertTrue(result['complete']);self.assertTrue(result['pdf_collected'])
        self.assertFalse(result['assets_observed']);self.assertEqual(result['assets_missing'],['usd_cash'])

    def test_current_only_nameless_page_can_use_independent_post_owner_check(self):
        self.fixture.mocks['auto_login.authenticated_page'].side_effect=IdentityUnavailable('名義未表示')
        result=self.execute(documents=False)
        self.assertTrue(result['assets_observed'])
        self.fixture.mocks['sbi_views.verify_post_owner'].assert_called_once()
        self.fixture.mocks['auto_login.collect'].assert_not_called()

    def test_contradictory_current_values_never_become_a_successful_capture(self):
        self.reader.side_effect=ObservationConflict('anonymous conflicting values')
        with self.assertRaises(AcquisitionError) as error:self.execute()
        self.assertEqual(error.exception.code,'SUMMARY_VALUES_CONFLICT')
        self.fixture.mocks['acquisition_journal.persist_capture'].assert_not_called()

    def test_missing_all_current_fields_does_not_turn_unknown_into_zero(self):
        self.status.update(complete=False,available=[],missing=list(('cash_total','usd_cash','growth_remaining','tsumitate_remaining')),
                           values={},usd_cash_observed=False)
        result=self.execute()
        self.assertTrue(result['pdf_collected']);self.assertFalse(result['assets_observed'])
        self.assertEqual(result['current_status']['values'],{})
