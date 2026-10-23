"""Anonymous legacy-summary scope and monthly measurement contracts."""
import unittest
from monthly_summary import parse_monthly_summary,_page_clock
from sbi_views import ObservationConflict


HEADER='| 保有資産評価\n\t\n詳細'
ROWS=[['| 保有資産評価\n\t\n詳細'],[''],
      ['現金残高等（合計）','3,400\u00a0'],['SBI証券口座分','0'],
      ['スィープ専用銀行口座','3,400'],['株式','1,200'],[''],['計','4,600']]
TEXT='''口座サマリー
| NISA投資可能枠
詳細
投資可能枠(2026年) －NISA(成長投資枠)
575
投資可能枠(2026年) －NISA(つみたて投資枠)
1,200,000
| 保有資産評価
詳細
現金残高等（合計）
3,400
SBI証券口座分
0
スィープ専用銀行口座
3,400
株式
1,200
計
4,600
| ポートフォリオ
株式
999,999
保有株数
999,999
計
999,999
'''


class MonthlySummaryParsing(unittest.TestCase):
    def test_captured_legacy_shape_reads_scoped_stocks_cash_and_optional_nisa(self):
        values=parse_monthly_summary(TEXT,[ROWS,[['保有資産評価','詳細']]])
        self.assertEqual(values['summary_stocks'],1200)
        self.assertEqual(values['cash_total'],3400)
        self.assertEqual(values['broker_cash'],0)
        self.assertEqual(values['bank'],3400)
        self.assertEqual(values['summary_total'],4600)
        self.assertEqual(values['growth_remaining'],{'year':2026,'remaining':575})
        self.assertEqual(values['tsumitate_remaining']['remaining'],1200000)
        self.assertNotIn('domestic',values);self.assertNotIn('foreign',values)

    def test_headed_amount_table_is_not_replaced_by_later_header_only_table(self):
        self.assertEqual(parse_monthly_summary('',[ROWS,[['保有資産評価','詳細']]])['summary_stocks'],1200)

    def test_header_only_table_does_not_capture_later_portfolio_table(self):
        values=parse_monthly_summary('',[[['保有資産評価','詳細']],[['株式','999'],['計','999']]])
        self.assertEqual(values,{})

    def test_flattened_body_cannot_extend_header_only_table_scope(self):
        text='保有資産評価\n詳細\n現金残高等(合計)\t100\n株式\t999\n計\t1099'
        values=parse_monthly_summary(text,[[['保有資産評価','詳細']],[['現金残高等(合計)','100'],['株式','999'],['計','1099']]])
        self.assertEqual(values,{})

    def test_text_fallback_stops_at_portfolio_heading_variants(self):
        for heading in ('ポートフォリオ一覧','株式（現物）'):
            with self.subTest(heading=heading):
                values=parse_monthly_summary('保有資産評価\n現金残高等(合計)\n100\n'+heading+'\n株式\n999')
                self.assertEqual(values,{'cash_total':100})

    def test_ancestor_layout_cell_is_not_a_scoped_valuation_table(self):
        self.assertEqual(parse_monthly_summary('',[[[TEXT]]]),{})

    def test_buying_power_and_unscoped_stock_quantity_are_ignored(self):
        self.assertEqual(parse_monthly_summary('買付余力\n999,999\n株式\n1,200\n現金残高等(合計)\n3,400'),{})

    def test_help_heading_does_not_start_scope(self):
        self.assertEqual(parse_monthly_summary('保有資産評価について\n株式\n100\n現金残高等(合計)\n200'),{})

    def test_explicit_zero_balances_remain_known_zero(self):
        table=[[HEADER],['現金残高等(合計)','0'],['SBI証券口座分','0'],['スイープ専用銀行口座','0'],['株式','0'],['計','0']]
        values=parse_monthly_summary('',[table])
        self.assertEqual(values,dict(cash_total=0,broker_cash=0,bank=0,summary_stocks=0,summary_total=0))

    def test_missing_stock_is_not_reconstructed_from_total_minus_cash(self):
        table=[[HEADER],['現金残高等(合計)','200'],['株式','--'],['計','200']]
        values=parse_monthly_summary('',[table])
        self.assertNotIn('summary_stocks',values);self.assertEqual(values['cash_total'],200)

    def test_missing_bank_is_not_reconstructed_from_total_minus_broker(self):
        values=parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['SBI証券口座分','0'],['株式','100']]])
        self.assertNotIn('bank',values)

    def test_cash_total_must_match_both_explicit_parts(self):
        with self.assertRaises(ObservationConflict):
            parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['SBI証券口座分','10'],['スイープ専用銀行口座','200']]])

    def test_only_stock_cash_total_is_reconciled(self):
        with self.assertRaises(ObservationConflict):
            parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['株式','100'],['計','301']]])

    def test_explicit_other_asset_class_does_not_force_two_component_total(self):
        values=parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['株式','100'],['投資信託','50'],['計','350']]])
        self.assertEqual(values['summary_total'],350)
        self.assertEqual(values['summary_stocks'],100)

    def test_extra_asset_cannot_make_total_smaller_than_already_known_components(self):
        with self.assertRaises(ObservationConflict):
            parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['株式','100'],['投資信託','50'],['計','250']]])

    def test_explicit_foreign_unit_prevents_bare_amounts_from_becoming_yen(self):
        for unit in ('単位：米ドル','単位(USD)','単位:USドル','単位:EUR','単位：ユーロ','単位：千円','単位：万円'):
            with self.subTest(unit=unit),self.assertRaises(ObservationConflict):
                parse_monthly_summary('',[[[HEADER],[unit],['株式','100'],['現金残高等(合計)','200']]])

    def test_explicit_yen_unit_is_allowed_without_scaling(self):
        for unit in ('単位：円','(単位:JPY)','単位:日本円'):
            with self.subTest(unit=unit):
                values=parse_monthly_summary('',[[[HEADER],[unit],['株式','100'],['現金残高等(合計)','200']]])
                self.assertEqual(values,{'summary_stocks':100,'cash_total':200})

    def test_inline_other_asset_class_does_not_force_two_component_total(self):
        values=parse_monthly_summary(HEADER+'\n現金残高等(合計)\t200\n株式\t100\n投信\t50\n計\t350')
        self.assertEqual(values['summary_total'],350)

    def test_unknown_additional_numeric_category_prevents_false_total_assertion(self):
        values=parse_monthly_summary('',[[[HEADER],['現金残高等(合計)','200'],['株式','100'],['別の商品区分','50'],['計','350']]])
        self.assertEqual(values['summary_total'],350)

    def test_duplicate_conflicting_scoped_valuation_tables_are_rejected(self):
        changed=[list(row) for row in ROWS];changed[5][1]='1,201'
        with self.assertRaises(ObservationConflict):parse_monthly_summary('',[ROWS,changed])

    def test_dollars_percentages_and_gains_are_not_stock_valuation(self):
        for raw in ('100 USD','10%','+100円','-100円'):
            with self.subTest(raw=raw):
                values=parse_monthly_summary('',[[[HEADER],['株式',raw],['現金残高等(合計)','200']]])
                self.assertNotIn('summary_stocks',values)

    def test_page_clock_accepts_real_legacy_hyphens_and_normalizes_for_journal(self):
        self.assertEqual(_page_clock('2026-10-23 15:57:00'),'2026/10/23 15:57:00')
        self.assertEqual(_page_clock('2026/1/2 9:01'),'2026/01/02 09:01')

    def test_ambiguous_or_invalid_page_clock_remains_unknown(self):
        for text in ('2026-10-23 15:57:00\n2026-10-23 15:58:00','2026-02-30 15:57:00','2026-10-23 25:00','2026-10-23'):
            with self.subTest(text=text):self.assertIsNone(_page_clock(text))


if __name__=='__main__':unittest.main()
