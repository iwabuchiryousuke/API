"""Offline acquisition regressions; synthetic amounts preserve captured SBI layouts."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfoNotFoundError

from ledger import Store, LedgerError
from login_credentials import CredentialError
from asset_snapshots import extract_observations, combine_assets, money, save, snapshots
from sbi_views import (parse_summary, parse_my_assets, parse_currency_summary,
                       capture_account, capture_status, latest, save_observation,
                       _jst_now, _check_asset_identity)

# Captured October 2026 layout: spacer lines, NBSP, full-width minus and
# parenthesized labels. All values and customer information are synthetic.
SUMMARY = '''口座サマリー
| NISA投資可能枠
\t
 詳細

投資可能枠(2026年) \u00a0\u00a0－NISA(成長投資枠)
\t
0\u00a0

投資可能枠(2026年) \u00a0\u00a0－NISA(つみたて投資枠)
\t
1,200,000\u00a0

現金残高等（合計）
\t
4,000\u00a0

    SBI証券口座分
\t
1,000\u00a0

    スィープ専用銀行口座
\t
3,000\u00a0
買付余力(2営業日後)
999,999
'''
ASSETS = '''国内株式
外国株式
投資信託
資産残高
2026/10/21 21:50
資産構成比率
国内株式(現物)
スィープ専用銀行口座
商品
評価額
評価損益
評価損益率
前日比/率(%)
前月比/率(%)
国内株式(現物)

5,000円

+2,000円

+66.67%

+100円

+2.04%

スィープ専用銀行口座

3,000円

--

--

0円

0.00%
合計
8,000円
'''


def fake_page():
    page = Mock()
    page.url = 'https://site.sbisec.co.jp/account/assets'
    page.is_closed.return_value = False
    page.frames = [page]
    page.get_by_role.return_value.count.return_value = 0
    page.locator.return_value.count.return_value = 0
    return page


class AssetParsing(unittest.TestCase):
    def test_captured_summary_spacing_and_zero_are_preserved(self):
        value = parse_summary(SUMMARY)
        self.assertEqual(value['cash_total'], 4000)
        self.assertEqual(value['growth_remaining'], {'year': 2026, 'remaining': 0})
        self.assertEqual(value['tsumitate_remaining']['remaining'], 1200000)
        self.assertNotIn('buying_power', value)

    def test_summary_table_rows_do_not_need_body_line_breaks(self):
        values = parse_summary('', [[['投資可能枠(2026年)－NISA(成長投資枠)', '12,300'],
                                    ['現金残高等（合計）', '6,000']]])
        self.assertEqual(values['growth_remaining']['remaining'], 12300)
        self.assertEqual(values['cash_total'], 6000)

    def test_cash_total_must_match_observed_parts(self):
        with self.assertRaises(LedgerError):
            parse_summary(SUMMARY.replace('4,000', '4,001'))

    def test_incompatible_nisa_years_do_not_merge(self):
        with self.assertRaises(LedgerError):
            parse_summary(SUMMARY + '\n投資可能枠(2027年)－NISA(成長投資枠)\n0')

    def test_captured_div_rows_ignore_menu_and_gain(self):
        value = parse_my_assets(ASSETS, [])
        self.assertEqual(value, {'domestic': 5000, 'bank': 3000})
        self.assertIsNone(combine_assets(value)['domestic'])
        self.assertIsNone(combine_assets(value)['foreign'])
        self.assertIsNone(combine_assets(value)['cash'])

    def test_menu_alone_is_not_an_asset_screen(self):
        with self.assertRaises(LedgerError):
            parse_my_assets('国内株式\n外国株式\n投資信託\n口座管理', [])

    def test_usd_native_amount_is_never_a_yen_amount(self):
        value = parse_my_assets('外国株式(米国)\n120 USD\n預り金(米ドル)\n10.25 USD', [])
        self.assertEqual(value['usd_cash'], 10.25)
        self.assertNotIn('foreign', value)
        self.assertNotIn('foreign_cash', value)

    def test_currency_table_selects_balance_not_buying_power(self):
        rows = [['通貨', '買付余力', '預り金', '円換算額'],
                ['米ドル', '9,999.00', '10.25', '1,500']]
        value = parse_currency_summary('', [rows])
        self.assertEqual(value, {'usd_cash': 10.25, 'foreign_cash': 1500})

    def test_cash_currency_label_allows_native_bare_amount(self):
        self.assertEqual(parse_currency_summary('米ドル預り金\n12.34')['usd_cash'], 12.34)
        self.assertNotIn('foreign_cash', parse_currency_summary('米ドル預り金\n12.34'))

    def test_zero_dollars_is_exactly_zero_yen_without_fx_inference(self):
        self.assertEqual(parse_currency_summary('米ドル預り金\n0'),
                         {'usd_cash': 0.0, 'foreign_cash': 0.0})

    def test_buying_power_and_percent_never_replace_balances(self):
        with self.assertRaises(LedgerError):
            parse_currency_summary('米ドル買付余力\n1,000 USD')
        self.assertIsNone(money('+1,000円'))
        self.assertIsNone(money('1,000%'))
        self.assertIsNone(money('1,2,3円'))

    def test_multiple_yen_values_in_one_cell_are_ambiguous(self):
        rows = [['商品', '評価額(円)'], ['外国株式(米国)', '100円\n200円']]
        self.assertNotIn('foreign', extract_observations('', [rows]))

    def test_native_and_yen_in_one_value_cell(self):
        rows = [['商品', '評価額'], ['外国株式(米国)', '10 USD\n1,500円']]
        self.assertEqual(extract_observations('', [rows])['foreign'], 1500)

    def test_conflicting_duplicate_currency_rows_fail(self):
        with self.assertRaises(LedgerError):
            parse_currency_summary('米ドル預り金\n1 USD\n米ドル預り金\n2 USD')


class AssetAcquisition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.page = fake_page()
        self.enterContext(patch('sbi_views.select_asset_page', side_effect=lambda page, **_: page))
        def immediate(page, kind, **kwargs):
            from sbi_views import snapshot_dom,parse_summary,parse_my_assets,parse_currency_summary
            text,tables=snapshot_dom(page)
            parser={'口座サマリー':parse_summary,'My資産':parse_my_assets,'口座(外貨建)':parse_currency_summary}[kind]
            return text,tables,parser(text,tables)
        self.enterContext(patch('sbi_asset_runtime.read_current_view',side_effect=immediate))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_missing_windows_tzdata_preserves_jst_offset(self):
        with patch('sbi_views.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            now = _jst_now()
            self.assertEqual(now.utcoffset(), timedelta(hours=9))
            save_observation(self.store, 'A', '口座サマリー', SUMMARY, parse_summary(SUMMARY))
        self.assertTrue(latest(self.store, '2099-01-01')[('A', '口座サマリー')]['observed_at'].endswith('+09:00'))

    def test_identity_missing_or_conflicting_aborts_before_observation(self):
        with patch('sbi_views.snapshot_dom', return_value=('ログアウト 口座管理', [])):
            with self.assertRaises(CredentialError):
                capture_account(self.page, self.store, 'A', owner='架空 太郎')
        self.assertFalse(latest(self.store, '2099-01-01'))
        with patch('sbi_views.snapshot_dom', return_value=('岩渕亮介 様 ログアウト', [])):
            with self.assertRaises(CredentialError):
                _check_asset_identity(self.page, '架空太郎')

    def test_explicit_prior_identity_allows_nameless_balance_page(self):
        values = {'口座サマリー': (SUMMARY, []), 'My資産': (ASSETS, []),
                  '口座(外貨建)': ('米ドル預り金\n10.25 USD', [])}
        state = ['口座サマリー']
        def nav(page, names, **kwargs):
            if names[0] != '口座管理':
                state[0] = names[0]
            return page
        with patch('sbi_views.navigate', side_effect=nav), \
             patch('sbi_views.snapshot_dom', side_effect=lambda _: values[state[0]]), \
             patch('sbi_views.include_cash'):
            complete = capture_account(self.page, self.store, 'A', owner='架空太郎', identity_verified=True)
        self.assertFalse(complete)
        observed = latest(self.store, '2099-01-01')
        self.assertEqual(observed[('A', 'My資産')]['values']['domestic'], 5000)
        self.assertEqual(observed[('A', 'My資産')]['values']['usd_cash'], 10.25)
        self.assertNotIn('foreign_cash', observed[('A', 'My資産')]['values'])
        status = capture_status(self.store, 'A')
        self.assertIn('foreign', status['missing'])
        self.assertIn('foreign_cash', status['missing'])
        self.assertTrue(status['usd_cash_observed'])

    def test_cancel_before_navigation_leaves_database_untouched(self):
        with patch('sbi_views.navigate') as nav:
            with self.assertRaises(CredentialError):
                capture_account(self.page, self.store, 'A', cancelled=lambda: True)
            nav.assert_not_called()
        self.assertFalse(latest(self.store, '2099-01-01'))

    def test_old_observations_do_not_count_as_current_coverage(self):
        then = datetime(2026, 10, 20, 12, tzinfo=timezone(timedelta(hours=9)))
        now = datetime(2026, 10, 21, 12, tzinfo=timezone(timedelta(hours=9)))
        with patch('sbi_views._jst_now', return_value=then):
            save_observation(self.store, 'A', '口座サマリー', SUMMARY, parse_summary(SUMMARY))
        with patch('sbi_views._jst_now', return_value=now):
            self.assertEqual(capture_status(self.store, 'A')['available'], [])

    def test_same_day_manual_snapshot_is_never_overwritten(self):
        day = _jst_now().date().isoformat()
        save(self.store, 'A', day, {'domestic': 999}, source='手動確認')
        with patch('sbi_views.navigate', return_value=self.page), \
             patch('sbi_views.snapshot_dom', return_value=(SUMMARY, [])), \
             patch('sbi_views.include_cash'):
            capture_account(self.page, self.store, 'A', identity_verified=True)
        self.assertEqual(snapshots(self.store)['A', day]['domestic'], 999)
        self.assertEqual(snapshots(self.store)['A', day]['source'], '手動確認')


if __name__ == '__main__':
    unittest.main()
