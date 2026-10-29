"""Daily grouping, source preservation and running formula regression cases."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from activity import Activities
from ledger import LedgerError, Snapshot, Store
from trade_report import (GroupedStore, _literal_criteria, _previous_average, build_trade_rows,
                          build_trade_sheet, group_trades)


def trade(day='2026-10-02', account='A', ident='9900', custody='特定', side='買',
          quantity='1', amount='100', **extra):
    row = dict(trade_date=day, account=account, account_label='Example '+account,
               name='Example Security', code=ident, custody=custody, side=side,
               quantity=quantity, settlement_amount=amount, price='100', fees='0',
               tax='0', market='東証', settlement_date=day, asset_type='国内株式',
               quantity_unit='株', price_unit='円/株', currency='JPY')
    row.update(extra)
    return row


class ReadOnlyStore:
    def __init__(self, directory, records=(), labels=None):
        self.directory=Path(directory);self.rows=deepcopy(list(records))
        self.profiles=[dict(id=a,label=(labels or {}).get(a,'Example '+a),owner_name='Owner '+a) for a in 'ABCDE']
    def records(self,month=''):
        return deepcopy([row for row in self.rows if not month or row['trade_date'][:7]<=month])
    def accounts(self):return deepcopy(self.profiles)


class ReadOnlyActivities:
    def __init__(self, events=()):self.rows=deepcopy(list(events))
    def events(self,month=''):return deepcopy(self.rows)


def cells(sheet):return {(cell['r'],cell['c']):cell for cell in sheet['cells']}


class DailyTradeGrouping(unittest.TestCase):
    def test_requested_same_day_conditions_merge_quantities_cash_and_charges(self):
        original=[trade(quantity='10',amount='1010',fees='1',tax='2',record_id='raw-1'),
                  trade(quantity='20',amount='1990',fees='3',tax='4',record_id='raw-2')]
        before=deepcopy(original);result=group_trades(original)
        self.assertEqual(original,before)
        self.assertEqual(len(result),1)
        row=result[0]
        self.assertEqual(Decimal(row['quantity']),30)
        self.assertEqual(Decimal(row['settlement_amount']),3000)
        self.assertEqual(Decimal(row['price']),100)
        self.assertEqual(Decimal(row['fees']),4);self.assertEqual(Decimal(row['tax']),6)
        self.assertEqual(row['source_record_ids'],['raw-1','raw-2'])
        self.assertEqual(row['source_count'],2)

    def test_each_grouping_condition_keeps_a_distinct_bucket(self):
        rows=[trade(),trade(day='2026-10-03'),trade(account='B'),trade(ident='9901'),
              trade(custody='NISA(成)'),trade(side='売')]
        self.assertEqual(len(group_trades(rows)),6)

    def test_identical_executions_are_summed_not_discarded_as_duplicates(self):
        rows=[trade(record_id='one'),trade(record_id='two')]
        result=group_trades(rows)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['source_count'],2)
        self.assertEqual(Decimal(result[0]['quantity']),2)
        self.assertEqual(Decimal(result[0]['settlement_amount']),200)

    def test_security_code_normalization_matches_excel_case_insensitive_criteria(self):
        result=group_trades([trade(ident='123a'),trade(ident='１２３Ａ')])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['code'],'123A')
        self.assertEqual(result[0]['source_count'],2)

    def test_codeless_funds_use_normalized_name_instead_of_merging_all_empty_codes(self):
        fund=dict(asset_type='投資信託',quantity_unit='口',price_unit='円/1万口')
        rows=[trade(ident='',name='Example Fund A',**fund),
              trade(ident='',name='Ｅｘａｍｐｌｅ　Ｆｕｎｄ　Ａ',**fund),
              trade(ident='',name='Example Fund B',**fund)]
        grouped=group_trades(rows)
        self.assertEqual(len(grouped),2)
        self.assertEqual(sorted(row['source_count'] for row in grouped),[1,2])

    def test_units_or_currency_cannot_be_silently_mixed_in_one_group(self):
        for altered in (dict(currency='USD'),dict(quantity_unit='口'),dict(price_unit='円/100株'),
                        dict(asset_type='投資信託')):
            with self.subTest(altered=altered),self.assertRaises(LedgerError):
                group_trades([trade(),trade(**altered)])

    def test_quote_aliases_for_the_same_fund_convention_are_compatible(self):
        common=dict(ident='',name='Example Fund',asset_type='投資信託',quantity_unit='口')
        result=group_trades([trade(**common,price_unit='円/1万口',quantity='1000',amount='101'),
                             trade(**common,price_unit='円/10000口',quantity='2000',amount='199')])
        self.assertEqual(len(result),1)
        self.assertEqual(Decimal(result[0]['price']),1000)

    def test_single_unsupported_currency_or_unit_never_appears_in_yen_columns(self):
        for altered in (dict(currency='USD'),dict(asset_type='外国株式'),
                        dict(quantity_unit='口'),dict(price_unit='円/100株'),
                        dict(asset_type='投資信託',quantity_unit='株',price_unit='円/1万口')):
            with self.subTest(altered=altered),self.assertRaises(LedgerError):group_trades([trade(**altered)])

    def test_mixed_settlement_dates_are_retained_without_inventing_one_date(self):
        result=group_trades([trade(settlement_date='2026-10-05'),trade(settlement_date='2026-10-06')])[0]
        self.assertEqual(result['settlement_date'],'')
        self.assertEqual(result['settlement_dates'],['2026-10-05','2026-10-06'])

    def test_invalid_quantity_and_nonfinite_cash_fail_without_mutating_input(self):
        for field,value in (('quantity','0'),('quantity','-1'),('quantity',True),
                            ('settlement_amount','NaN'),('fees','Infinity'),('tax','-1')):
            rows=[trade(**{field:value})];before=deepcopy(rows)
            with self.subTest(field=field,value=value),self.assertRaises(LedgerError):group_trades(rows)
            self.assertEqual(rows,before)

    def test_grouped_store_is_a_read_only_adapter_with_month_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            source=ReadOnlyStore(directory,[trade(),trade(),trade(day='2026-11-01')])
            proxy=GroupedStore(source)
            self.assertEqual(len(proxy.records('2026-10')),1)
            self.assertEqual(len(source.records()),3)
            self.assertEqual(proxy.directory,source.directory)


class RunningTradeAccounting(unittest.TestCase):
    def test_daily_aggregation_precedes_ceil_and_avoids_per_execution_extra_yen(self):
        records=[trade(day='2026-10-01',quantity='100',amount='10000'),
                 trade(quantity='10',amount='1010'),trade(quantity='10',amount='990')]
        rows=build_trade_rows(records)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['calculated_quantity'],120)
        self.assertEqual(rows[0]['calculated_average'],100)
        # Sequential execution rounding would produce 101; the daily result is 100.
        sequential=((Decimal(100)*100+1010)/110).to_integral_value(rounding='ROUND_CEILING')
        sequential=((sequential*110+990)/120).to_integral_value(rounding='ROUND_CEILING')
        self.assertEqual(sequential,101)

    def test_daily_sale_receipts_and_quantity_produce_one_net_realized_gain(self):
        records=[trade(day='2026-10-01',quantity='100',amount='10000'),
                 trade(side='売',quantity='50',amount='5000'),
                 trade(side='売',quantity='20',amount='2500')]
        rows=build_trade_rows(records)
        self.assertEqual(rows[0]['source_count'],2)
        self.assertEqual(rows[0]['calculated_quantity'],30)
        self.assertEqual(rows[0]['calculated_average'],100)
        self.assertEqual(rows[0]['calculated_gain'],500)

    def test_same_day_buy_precedes_sale_in_daily_accounting_and_descending_rows(self):
        records=[trade(day='2026-10-01',quantity='1',amount='10'),
                 trade(side='売',quantity='2',amount='30'),
                 trade(quantity='2',amount='20')]
        rows=build_trade_rows(records)
        self.assertEqual([row['side'] for row in rows],['売','買','買'])
        self.assertEqual(rows[0]['calculated_quantity'],1)
        self.assertEqual(rows[0]['calculated_gain'],10)

    def test_split_stays_separate_from_same_day_purchase_and_does_not_add_cash(self):
        records=[trade(day='2026-10-01',quantity='1',amount='100'),trade(quantity='1',amount='60')]
        event=dict(type='株式分割',date='2026-10-02',account='A',account_label='Example A',
                   code='9900',name='Example Security',before=1,added=1,after=2,
                   allocations={'特定':[1,1,2]})
        rows=build_trade_rows(records,[event])
        self.assertEqual([row['movement'] for row in rows],['trade','split','trade'])
        self.assertEqual(rows[1]['settlement_amount'],'0')
        self.assertEqual(rows[1]['calculated_average'],50)
        self.assertEqual(rows[0]['calculated_quantity'],3)
        self.assertEqual(rows[0]['calculated_average'],54)

    def test_generic_nisa_split_uses_the_unique_existing_custody_bucket(self):
        record=trade(day='2026-10-01',custody='NISA(成)',quantity='1',amount='100')
        event=dict(type='株式分割',date='2026-10-02',account='A',code='9900',name='Example',
                   before=1,added=1,after=2,allocations={'NISA':[1,1,2]})
        rows=build_trade_rows([record],[event])
        self.assertEqual(rows[0]['custody'],'NISA(成)')
        self.assertTrue(rows[0]['basis_known'])
        self.assertEqual(rows[0]['calculated_average'],50)

    def test_incomplete_opening_cost_never_becomes_known_by_an_accidental_net_zero(self):
        records=[trade(day='2026-10-01',side='売',quantity='1',amount='200'),
                 trade(day='2026-10-02',quantity='1',amount='100'),
                 trade(day='2026-10-03',quantity='1',amount='110')]
        rows=build_trade_rows(records)
        self.assertTrue(all(not row['basis_known'] for row in rows))
        self.assertTrue(all(row['calculated_average'] is None for row in rows))
        self.assertTrue(all(row['calculated_gain'] is None for row in rows))

    def test_dividend_before_first_trade_marks_missing_opening_history(self):
        records=[trade(day='2026-10-02')]
        event=dict(type='配当',date='2026-09-01',account='A',code='9900')
        self.assertFalse(build_trade_rows(records,[event])[0]['basis_known'])

    def test_fund_average_and_gain_retain_per_ten_thousand_quote_unit(self):
        fund=dict(asset_type='投資信託',quantity_unit='口',price_unit='円/1万口')
        rows=build_trade_rows([trade(day='2026-10-01',quantity='10000',amount='1234',**fund),
                               trade(side='売',quantity='5000',amount='700',**fund)])
        self.assertEqual(rows[0]['quote_unit'],10000)
        self.assertEqual(rows[0]['calculated_average'],1234)
        self.assertEqual(rows[0]['calculated_gain'],83)

    def test_explicit_per_unit_fund_quote_is_not_multiplied_by_ten_thousand(self):
        fund=dict(asset_type='投資信託',quantity_unit='口',price_unit='円/口')
        rows=build_trade_rows([trade(day='2026-10-01',quantity='10',amount='123',**fund),
                               trade(side='売',quantity='5',amount='70',**fund)])
        self.assertEqual(rows[0]['quote_unit'],1)
        self.assertEqual(rows[1]['price'],'12.3')
        self.assertEqual(rows[0]['calculated_average'],13)
        self.assertEqual(rows[0]['calculated_gain'],5)

    def test_currency_or_unit_change_across_days_is_not_hidden_by_daily_grouping(self):
        with self.assertRaises(LedgerError):
            build_trade_rows([trade(day='2026-10-01'),trade(day='2026-10-02',currency='USD')])


class TradeSheetPresentation(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name)

    def build(self,records=(),events=(),capture=None,labels=None):
        return build_trade_sheet(ReadOnlyStore(self.directory,records,labels),ReadOnlyActivities(events),
                                 '2026-10-31',current_capture=capture)

    def test_sample_columns_and_formula_cells_have_no_external_workbook_links(self):
        sh=self.build([trade(day='2026-10-01'),trade(side='売')]);mapping=cells(sh)
        self.assertEqual([mapping[1,c]['v'] for c in range(11,14)],['保有株数','平均取得単価','確定損益'])
        self.assertEqual(mapping[1,16]['v'],'成長枠残高')
        self.assertEqual(mapping[1,19]['v'],'現金(ドル)')
        for r in (2,3):
            for c in (11,12,13):
                self.assertEqual(mapping[r,c]['k'],'formula_number')
                self.assertNotIn('[1]',mapping[r,c]['v'])
                self.assertNotIn('_xlfn',mapping[r,c]['v'])
        self.assertIn('SUMIFS',mapping[2,11]['v'])
        self.assertIn('ROUNDUP',mapping[2,12]['v'])
        self.assertIn('INDEX',mapping[2,13]['v'])
        self.assertEqual(sh['widths']['12'],9.5);self.assertEqual(sh['row_height'],20.1)
        json.dumps(sh,ensure_ascii=False,allow_nan=False)

    def test_unknown_cost_stays_a_blank_formula_instead_of_a_guessed_gain(self):
        mapping=cells(self.build([trade(side='売')]))
        self.assertEqual(mapping[2,12]['v'],'IF(A2="","","")')
        self.assertEqual(mapping[2,13]['v'],'IF(A2="","","")')

    def test_current_capture_uses_jst_date_native_usd_zero_and_current_nisa_year(self):
        capture=dict(captured_at='2026-10-28T16:30:00+00:00',accounts={
            'A':dict(observed_at='2026-10-28T16:30:00+00:00',values=dict(cash_total=0,usd_cash=Decimal('12.34'),
                growth_remaining=dict(year=2026,remaining=0),tsumitate_remaining=dict(year=2025,remaining=100))),
            'E':dict(observed_at='2026-10-28T16:30:00+00:00',values=dict(cash_total=999,usd_cash=888))})
        mapping=cells(self.build(capture=capture))
        self.assertEqual(mapping[1,15]['v'],'2026-10-29')
        self.assertEqual(mapping[2,16]['v'],0);self.assertIsNone(mapping[2,17]['v'])
        self.assertEqual(mapping[2,18]['v'],0);self.assertEqual(mapping[2,19]['v'],12.34)
        self.assertIn('.00',mapping[2,19]['format'])
        for c in range(16,20):self.assertIsNone(mapping[6,c]['v'])

    def test_no_capture_never_falls_back_to_an_old_snapshot_or_export_date(self):
        mapping=cells(self.build())
        self.assertIsNone(mapping[1,15]['v'])
        for r in range(2,7):
            for c in range(16,20):self.assertIsNone(mapping[r,c]['v'])

    def test_same_named_accounts_remain_distinct_in_excel_formula_criteria(self):
        mapping=cells(self.build([trade(account='A'),trade(account='B')],labels={'A':'Same','B':'Same'}))
        self.assertNotEqual(mapping[2,2]['v'],mapping[3,2]['v'])
        self.assertEqual({mapping[2,2]['v'],mapping[3,2]['v']},{'Same（A）','Same（B）'})

    def test_case_only_account_aliases_are_disambiguated_for_excel_sumifs(self):
        mapping=cells(self.build([trade(account='A'),trade(account='B')],labels={'A':'Alias','B':'alias'}))
        self.assertEqual({mapping[2,2]['v'],mapping[3,2]['v']},{'Alias（A）','alias（B）'})

    def test_account_wildcards_and_tilde_are_literal_sumifs_criteria(self):
        mapping=cells(self.build([trade(account='A'),trade(account='B')],
                                 labels={'A':'Account*','B':'Account Other'}))
        self.assertEqual({mapping[2,2]['v'],mapping[3,2]['v']},{'Account*','Account Other'})
        for r in (2,3):
            formula=mapping[r,11]['v']
            self.assertIn(_literal_criteria(f'B{r}'),formula)
            self.assertIn(_literal_criteria(f'D{r}'),formula)
            self.assertIn(_literal_criteria(f'E{r}'),formula)
            self.assertEqual(formula.count('"~","~~"'),6)
        self.assertTrue(_literal_criteria('B2').startswith('"="&'))

    def test_future_trades_are_excluded_by_the_report_boundary(self):
        mapping=cells(self.build([trade(day='2026-10-01'),trade(day='2026-11-01')]))
        self.assertEqual(mapping[2,1]['v'],'2026-10-01')
        self.assertNotIn((3,1),mapping)

    def test_formula_at_supported_last_row_does_not_reference_itself(self):
        self.assertEqual(_previous_average(10000),'0')

    def test_actual_ledger_export_never_rewrites_raw_transactions_or_evidence(self):
        store=Store(self.directory/'data');activities=Activities(store)
        try:
            rows=[trade(quantity='10',amount='1010'),trade(quantity='10',amount='990')]
            for row in rows:
                for key in ('account','account_label'):row.pop(key)
            raw=json.dumps(rows,sort_keys=True).encode()
            snapshot=Snapshot(raw,'anonymous.csv',hashlib.sha256(raw).hexdigest(),
                              '2026-10-02','2026-10-02',rows)
            store.import_snapshot('A',snapshot)
            before='\n'.join(store.db.iterdump());original=store.records()
            for _ in range(2):
                sh=build_trade_sheet(store,activities,'2026-10-31')
                self.assertEqual(cells(sh)[2,7]['v'],20)
                self.assertNotIn((3,1),cells(sh))
            self.assertEqual(store.records(),original)
            self.assertEqual('\n'.join(store.db.iterdump()),before)
        finally:store.close()


if __name__=='__main__':unittest.main()
