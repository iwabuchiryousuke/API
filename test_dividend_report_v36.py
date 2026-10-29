"""Compact v3.6 worksheet contracts using anonymous, isolated ledger data."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from dividend_report import build_dividend_sheet, SUMMARY_HEADERS
from test_data_compatibility import seed_fixture


def grid(sheet):
    return {(cell['r'], cell['c']): cell for cell in sheet['cells']}


class DividendReportV36(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store, self.activities = seed_fixture(self.base)
        self.addCleanup(self.store.close)

    def facts(self, value):
        (self.store.directory / 'market/research_2026.json').write_text(
            json.dumps({'year': 2026, 'securities': value}), encoding='utf-8')

    def build(self, **kwargs):
        return build_dividend_sheet(self.store, self.activities, '2026-10-31',
                                    today=date(2026, 10, 20), **kwargs)

    def test_detail_removes_fee_column_and_cash_items_have_only_account_date_type_amount(self):
        self.activities.add_cash('B', '税還付', '2026-10-19', '123', 'Old descriptive text')
        sheet = self.build(); cells = grid(sheet)
        self.assertEqual([cells[1, column]['v'] for column in range(1, 10)],
                         ['日付', '口座', '銘柄', 'コード', '預り', '分類', '数量', '単価（円）', '受取額（円）'])
        self.assertNotIn((1, 10), cells)
        for row in (row for row, column in cells if column == 6 and cells[row, column]['v'] in ('税還', '利息')):
            for column in (3, 4, 5, 7, 8):
                self.assertIsNone(cells[row, column]['v'])
            self.assertEqual(cells[row, 1]['k'], 'date')
            self.assertIsInstance(cells[row, 9]['v'], (int, float))
        self.assertNotIn('Old descriptive text', [cell['v'] for cell in sheet['cells']])

    def test_security_summary_is_contiguous_and_deleted_headings_are_absent(self):
        sheet = self.build(); cells = grid(sheet)
        self.assertEqual(tuple(cells[1, column]['v'] for column in range(11, 26)), SUMMARY_HEADERS)
        self.assertEqual(max(cell['c'] for cell in sheet['cells']), 25)
        for forbidden in ('手数料・税額（円）', '株価日', '出典・確認日', '銘柄別との差'):
            self.assertNotIn(forbidden, [cell['v'] for cell in sheet['cells']])
        self.assertEqual(cells[1, 24]['v'], '前月末日終値')
        self.assertEqual(cells[1, 25]['v'], '今月1日終値')

    def test_blocks_have_exactly_two_empty_rows_and_all_three_cumulative_sum_formulas(self):
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        self.assertEqual(blocks['cumulative_header_row'], blocks['summary_last_row'] + 3)
        self.assertEqual(blocks['calendar_label_row'], blocks['cumulative_last_row'] + 3)
        for boundary in (blocks['summary_last_row'], blocks['cumulative_last_row']):
            self.assertFalse(any((row, column) in cells for row in (boundary + 1, boundary + 2)
                                 for column in range(11, 26)))
        total = blocks['cumulative_total_row']
        self.assertEqual(cells[total, 11]['v'], '累計')
        for column in (12, 13, 14):
            self.assertEqual(cells[total, column]['k'], 'formula_number')
            self.assertTrue(cells[total, column]['v'].startswith('SUM('))
        self.assertEqual(cells[total + 1, 11]['v'], '総計')
        self.assertEqual(cells[total + 1, 12]['v'], f'SUM(L{total}:N{total})')

    def test_cumulative_years_include_older_registered_income_not_only_2024_onward(self):
        self.activities.add_cash('B', '利息', '2022-12-20', '7')
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        header = blocks['cumulative_header_row']
        total = blocks['cumulative_total_row']
        self.assertEqual([cells[row, 11]['v'] for row in range(header + 1, total)],
                         [2022, 2023, 2024, 2025, 2026])
        security_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
        self.assertNotIn('2024', cells[security_row, 22]['v'])

    def test_current_month_first_close_requires_exact_first_date(self):
        self.facts({'9900': dict(close=130, close_date='2026-09-30',
                                 month_start_close=132, month_start_close_date='2026-10-02')})
        cells = grid(self.build())
        row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
        self.assertEqual(cells[row, 24]['v'], 130)
        self.assertIsNone(cells[row, 25]['v'])
        self.facts({'9900': dict(close=130, close_date='2026-08-31',
                                 month_start_close=132, month_start_close_date='2026-10-01')})
        cells = grid(self.build())
        self.assertIsNone(cells[row, 24]['v'])
        self.assertEqual(cells[row, 25]['v'], 132)

    def test_zero_boolean_and_future_closing_quotes_remain_unobserved(self):
        for invalid in (0, True, False, -1):
            with self.subTest(invalid=invalid):
                self.facts({'9900': dict(close=invalid, close_date='2026-09-30',
                                         month_start_close=invalid, month_start_close_date='2026-10-01')})
                cells = grid(self.build())
                row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
                self.assertIsNone(cells[row, 24]['v']); self.assertIsNone(cells[row, 25]['v'])
        self.facts({'9900': dict(close=130, close_date='2026-09-30',
                                 month_start_close=132, month_start_close_date='2026-10-01')})
        cells = grid(build_dividend_sheet(self.store, self.activities, '2026-10-31', today=date(2026, 9, 30)))
        self.assertEqual(cells[row, 24]['v'], 130)
        self.assertIsNone(cells[row, 25]['v'])

    def test_calendar_is_chronological_without_a_column_header_row(self):
        self.facts({'9900': dict(rights='2026-11-27 / 2026-06-26',
                                 payments='2026-12-15 / 2026-09-15', earnings='2026-10-28')})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        label = blocks['calendar_label_row']
        self.assertEqual(cells[label, 11]['v'], 'カレンダー')
        self.assertFalse(any((label, column) in cells for column in (12, 13, 14)))
        rows = range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
        stamps = [cells[row, 11]['v'] for row in rows]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(cells[row, 11]['k'] == 'date' for row in rows))
        self.assertEqual(set(cells[row, 13]['v'] for row in rows),
                         {'権利付き最終日', '配当金支払日', '本決算発表日'})

    def test_actual_payment_combines_accounts_and_supersedes_research_forecast(self):
        self.activities.add_cash('B', '配当', '2026-10-15', '50', 'Example Security',
                                 code='9900', custody='NISA', quantity='50', per_share='1')
        self.facts({'9900': dict(calendar_complete=True, payments='2026-10-15',
                                 events=[dict(rights='2026-09-01', payment='2026-10-15', dps=10)])})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        rows = [row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                if cells[row, 11]['v'] == '2026-10-15' and cells[row, 13]['v'] == '配当金支払日']
        self.assertEqual(len(rows), 1)
        amount = cells[rows[0], 14]
        self.assertEqual(amount['k'], 'formula_number')
        self.assertIn('SUMIFS(', amount['v'])
        self.assertIn('（実績）', amount['format'])
        self.assertEqual(sum(cell['v'] for (row, column), cell in cells.items()
                             if column == 9 and row > 1 and cells[row, 6]['v'] == '配当'), 150)

    def test_unknown_future_payment_remains_blank_and_only_first_upcoming_rule_is_used(self):
        self.facts({'9900': dict(calendar_complete=False, rights='2026-11-10',
                                 payments='2026-11-20', earnings='2026-11-10')})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        future_payment = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                              if cells[row, 11]['v'] == '2026-11-20')
        self.assertNotIn((future_payment, 14), cells)
        self.assertEqual(len(sheet['conditional_formats']), 1)
        rule = sheet['conditional_formats'][0]
        self.assertEqual(rule['dxf'], 2)
        self.assertIn('COUNTIF(', rule['formula'])
        self.assertIn('TODAY()', rule['formula'])
        self.assertEqual(rule['range'], f'K{blocks["calendar_first_row"]}:N{blocks["calendar_last_row"]}')
        first_upcoming = blocks['calendar_highlight_row']
        self.assertEqual(cells[first_upcoming, 11]['v'], '2026-11-10')
        self.assertTrue(all(cell.get('fill') is None for cell in sheet['cells']))

    def test_verified_nisa_forecast_uses_remaining_entitled_shares_and_numeric_forecast_tag(self):
        self.facts({'9900': dict(calendar_complete=True,
                                 events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        payment = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                       if cells[row, 11]['v'] == '2026-12-15')
        self.assertEqual(cells[payment, 14]['k'], 'number')
        self.assertEqual(cells[payment, 14]['v'], 150)
        self.assertIn('（予想）', cells[payment, 14]['format'])
        summary_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
        self.assertEqual(cells[summary_row, 20]['v'], 150)

    def test_next_year_announced_payment_is_not_falsely_zero_from_current_year_forecast(self):
        self.facts({'9900': dict(calendar_complete=True,
                                 events=[dict(rights='2026-12-28', payment='2027-01-05',
                                              payment_year=2027, dps=2)])})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        payment = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                       if cells[row, 11]['v'] == '2027-01-05')
        self.assertNotIn((payment, 14), cells)

    def test_synthetic_fund_id_does_not_turn_unmatched_forecast_into_zero(self):
        import hashlib
        from ledger import Snapshot
        from report_legacy import reporting_code
        trade = dict(trade_date='2026-10-01', settlement_date='2026-10-05',
                     name='Anonymous Fund', code='', market='投信', side='買',
                     custody='NISA(成)', tax_class='', quantity='10000', price='100',
                     fees='0', tax='0', settlement_amount='100', asset_type='投資信託',
                     quantity_unit='口', price_unit='円/1万口')
        raw = json.dumps(trade).encode()
        self.store.import_snapshot('B', Snapshot(raw, 'fund.csv', hashlib.sha256(raw).hexdigest(),
                                                 '2026-10-01', '2026-10-01', [trade]))
        ident = reporting_code(trade)
        self.facts({ident: dict(calendar_complete=True,
                               events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        summary_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == ident)
        self.assertIsNone(cells[summary_row, 20]['v'])
        payment = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                       if cells[row, 11]['v'] == '2026-12-15')
        self.assertNotIn((payment, 14), cells)

    def test_numeric_fund_code_is_excluded_before_per_share_forecast_without_losing_actual_receipts(self):
        import hashlib
        from unittest.mock import patch
        from dividend_forecast import forecast
        from ledger import Snapshot
        trade = dict(trade_date='2026-10-01', settlement_date='2026-10-05',
                     name='Anonymous Coded Fund', code='9903', market='投信', side='買',
                     custody='NISA(成)', tax_class='', quantity='10000', price='100',
                     fees='0', tax='0', settlement_amount='100', asset_type='投資信託',
                     quantity_unit='口', price_unit='円/1万口')
        raw = json.dumps(trade).encode()
        self.store.import_snapshot('B', Snapshot(raw, 'coded-fund.csv', hashlib.sha256(raw).hexdigest(),
                                                 '2026-10-01', '2026-10-01', [trade]))
        self.activities.add_cash('B', '配当', '2026-10-20', '10', 'Anonymous Coded Fund',
                                 code='9903', custody='NISA(成)', quantity='10000', per_share='.001')
        self.facts({'9903': dict(calendar_complete=True, dps=2,
                                events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})
        with patch('dividend_report.forecast', wraps=forecast) as calculate_forecast:
            sheet = self.build()
        self.assertNotIn('9903', calculate_forecast.call_args.args[2])
        cells = grid(sheet); blocks = sheet['blocks']
        summary_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9903)
        self.assertIsNone(cells[summary_row, 20]['v'])
        self.assertEqual(cells[summary_row, 17]['v'], 2)
        receipt_row = next(row for row, column in cells if column == 4 and cells[row, column]['v'] == 9903)
        self.assertEqual(cells[receipt_row, 9]['v'], 10)
        payment = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                       if cells[row, 11]['v'] == '2026-12-15')
        self.assertNotIn((payment, 14), cells)

    def test_existing_manual_split_with_numeric_strings_is_reported_without_mutation(self):
        from split_entry import register
        event = register(self.store, self.activities, 'A', '9900', '2026-10-10', 1, 2,
                         'Anonymous split fixture')
        self.assertIsInstance(event['added'], str)
        before = '\n'.join(self.store.db.iterdump())
        cells = grid(self.build())
        summary_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
        self.assertEqual(cells[summary_row, 16]['k'], 'formula_number')
        self.assertEqual('\n'.join(self.store.db.iterdump()), before)

    def test_future_records_are_not_actual_income_or_calendar_payments_at_cutoff(self):
        self.activities.add_cash('B', '配当', '2026-12-15', '50', 'Example Security',
                                 code='9900', custody='NISA', quantity='50', per_share='1')
        sheet = self.build(); cells = grid(sheet)
        self.assertNotIn('2026-12-15', [cell['v'] for cell in cells.values() if cell['k'] == 'date'])

    def test_dividend_without_trade_history_does_not_invent_zero_holdings(self):
        self.activities.add_cash('B', '配当', '2026-10-15', '50', 'Other Security',
                                 code='9911', custody='NISA', quantity='50', per_share='1')
        cells = grid(self.build())
        row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9911)
        self.assertIsNone(cells[row, 16]['v'])

    def test_duplicate_aliases_are_disambiguated_consistently_with_trade_sheet(self):
        self.store.rename('A', '同名')
        self.store.rename('B', '同名')
        self.activities.add_cash('B', '税還付', '2026-10-19', '12')
        cells = grid(self.build())
        self.assertIn('同名（A）', [cell['v'] for (row, column), cell in cells.items() if row > 1 and column == 2])
        self.assertIn('同名（B）', [cell['v'] for (row, column), cell in cells.items() if row > 1 and column == 2])

    def test_security_name_override_applies_to_detail_summary_and_calendar_without_changing_source(self):
        from security_names import save
        save(self.store.directory, '9900', '表示用の短い名前')
        self.facts({'9900': dict(earnings='2026-11-10')})
        before = self.activities.events()[1]['name']
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        for (row, column), cell in cells.items():
            if row > 1 and column == 6 and cell['v'] == '配当':
                self.assertEqual(cells[row, 3]['v'], '表示用の短い名前')
        summary_row = next(row for row, column in cells if column == 11 and cells[row, column]['v'] == 9900)
        self.assertEqual(cells[summary_row, 12]['v'], '表示用の短い名前')
        for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1):
            self.assertEqual(cells[row, 12]['v'], '表示用の短い名前')
        self.assertEqual(self.activities.events()[1]['name'], before)

    def test_tied_upcoming_dates_choose_one_event_and_expired_only_calendar_has_none(self):
        self.facts({'9900': dict(rights='2026-11-10', earnings='2026-11-10', payments='2026-11-10')})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        first = blocks['calendar_highlight_row']
        self.assertEqual(cells[first, 11]['v'], '2026-11-10')
        self.assertEqual(cells[first, 13]['v'], '権利付き最終日')
        self.assertEqual(sum(cells[row, 11]['v'] == '2026-11-10'
                             for row in range(blocks['calendar_first_row'], first + 1)), 1)
        later = build_dividend_sheet(self.store, self.activities, '2026-10-31', today=date(2027, 1, 1))
        self.assertIsNone(later['blocks']['calendar_highlight_row'])
        self.assertTrue(all(cell.get('fill') is None for cell in later['cells']))

    def test_no_db_or_research_write_occurs_during_build(self):
        before = '\n'.join(self.store.db.iterdump())
        research = self.store.directory / 'market/research_2026.json'
        previous = research.read_bytes()
        self.build()
        self.assertEqual('\n'.join(self.store.db.iterdump()), before)
        self.assertEqual(research.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
