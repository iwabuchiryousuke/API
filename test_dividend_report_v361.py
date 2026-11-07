"""v3.6.1 dividend presentation, with isolated anonymous financial records."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dividend_report import SUMMARY_HEADERS, build_dividend_sheet
from test_data_compatibility import seed_fixture


def grid(sheet):
    return {(cell['r'], cell['c']): cell for cell in sheet['cells']}


class DividendReportV361(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store, self.activities = seed_fixture(self.base)
        self.addCleanup(self.store.close)

    def facts(self, facts, year=2026):
        path = self.store.directory / 'market' / f'research_{year}.json'
        path.write_text(json.dumps({'year': year, 'securities': facts}), encoding='utf-8')

    def build(self, as_of='2026-10-31', today=date(2026, 10, 20)):
        return build_dividend_sheet(self.store, self.activities, as_of, today=today)

    def summary_row(self, cells, ident=9900):
        return next(row for row, column in cells
                    if column == 11 and cells[row, column]['k'] == 'code'
                    and cells[row, column]['v'] == ident)

    def calendar_rows(self, sheet):
        blocks = sheet['blocks']
        return range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)

    def test_exact_template_headers_and_removed_status_column_pack_to_w_and_x(self):
        self.facts({'9900': {'status': 'This private research status must stay invisible',
                              'close': 130, 'close_date': '2026-09-30',
                              'month_start_close': 132, 'month_start_close_date': '2026-10-01',
                              'month_start_price_source': 'Verified fixture source'}})
        sheet = self.build(); cells = grid(sheet)
        expected = ('コード', '銘柄', '権利付き最終日', '配当金支払日', '本決算発表日',
                    '株数', 'DPS', '配当利回', '実績', '予想(税後)', '年間予想',
                    '累計配当金', '末日終値', '月初終値')
        self.assertEqual(SUMMARY_HEADERS, expected)
        self.assertEqual(tuple(cells[1, column]['v'] for column in range(11, 25)), expected)
        self.assertEqual((cells[1, 8]['v'], cells[1, 9]['v']), ('単価(円)', '受取額(円)'))
        self.assertEqual(max(cell['c'] for cell in sheet['cells']), 24)
        self.assertFalse(any('確認状態' == cell['v'] or 'private research' in str(cell['v'])
                             for cell in sheet['cells']))
        row = self.summary_row(cells)
        self.assertEqual((cells[row, 23]['v'], cells[row, 24]['v']), (130, 132))
        self.assertIn(f'Q{row}/W{row}', cells[row, 18]['v'])
        self.assertNotIn(f'X{row}', cells[row, 18]['v'])

    def test_summary_dates_use_current_jst_year_not_historical_financial_cutoff(self):
        self.activities.add_cash('A', '配当', '2025-06-20', '9', 'Example Security', code='9900')
        self.facts({'9900': {'rights': '2025-03-27・2025-09-26', 'payments': '2025-06-20'}}, 2025)
        self.facts({'9900': {'rights': '2026-09-28・2025-03-27・2026-03-27・2026-03-27',
                              'payments': '2026-06-20', 'earnings': '2026-01-29'}})
        cells = grid(self.build(as_of='2025-12-31')); row = self.summary_row(cells)
        self.assertEqual([cells[row, column]['v'] for column in (13, 14, 15)],
                         ['3/27・9/28', '6/20', '1/29'])
        self.assertIn('DATE(2025,1,1)', cells[row, 19]['v'])

    def test_current_year_uses_jst_midnight_for_aware_today(self):
        self.facts({'9900': {'rights': '2027-03-26', 'payments': '2027-06-20'}}, 2027)
        sheet = self.build(today=datetime(2026, 12, 31, 15, 30, tzinfo=timezone.utc))
        cells = grid(sheet); row = self.summary_row(cells)
        self.assertEqual(cells[row, 13]['v'], '3/26')
        self.assertEqual(sheet['blocks']['calendar_year'], 2027)
        self.assertEqual(cells[sheet['blocks']['calendar_label_row'], 11]['v'], 2027)

    def test_event_dates_fill_summary_without_top_level_duplicate_labels(self):
        self.facts({'9900': {'events': [
            {'rights': '2026-09-28', 'payment': '2026-12-20'},
            {'rights': '2026-03-27', 'payment': '2026-06-20'},
            {'rights': '2027-03-29', 'payment': '2027-06-20'}]}})
        cells = grid(self.build()); row = self.summary_row(cells)
        self.assertEqual(cells[row, 13]['v'], '3/27・9/28')
        self.assertEqual(cells[row, 14]['v'], '6/20・12/20')
        self.assertEqual(cells[row, 15]['v'], '未定')

    def test_verified_public_payment_merges_with_existing_rights_before_forecast_projection(self):
        from public_research import parse_investing_dividends, merge_investing_reference
        text = ('<h1>Anonymous Company (9900)</h1><table><tr><th>権利落ち日</th>'
                '<th>配当金支払日</th><th>配当</th></tr><tr><td>2026-11-26</td>'
                '<td>2026-12-15</td><td>2</td></tr></table>')
        reference = parse_investing_dividends(
            text, '9900', 2026, 'https://jp.investing.com/equities/example-dividends')
        facts = merge_investing_reference(
            {'calendar_complete': True, 'events': [{'rights': '2026-11-25', 'dps': 2}]},
            reference, 2026, '9900')
        self.facts({'9900': facts})
        sheet = self.build(); cells = grid(sheet); row = self.summary_row(cells)
        self.assertEqual([cells[row, column]['v'] for column in (13, 14)], ['11/25', '12/15'])
        payment = next(row for row in self.calendar_rows(sheet) if cells[row, 11]['v'] == '2026-12-15')
        self.assertEqual(cells[payment, 14]['v'], 150)
        self.assertEqual(cells[payment, 14]['format'], '#,##0')
        self.assertFalse(any('investing.com' in str(cell['v']) for cell in sheet['cells']))

    def test_public_ex_date_without_verified_rights_does_not_become_entitlement_or_zero_cash(self):
        from public_research import parse_investing_dividends, merge_investing_reference
        text = ('<h1>Anonymous Company (9900)</h1><table><tr><th>権利落ち日</th>'
                '<th>配当金支払日</th><th>配当</th></tr><tr><td>2026-11-26</td>'
                '<td>2026-12-15</td><td>2</td></tr></table>')
        reference = parse_investing_dividends(
            text, '9900', 2026, 'https://jp.investing.com/equities/example-dividends')
        self.facts({'9900': merge_investing_reference({}, reference, 2026, '9900')})
        sheet = self.build(); cells = grid(sheet); row = self.summary_row(cells)
        self.assertEqual(cells[row, 13]['v'], '未定')
        self.assertEqual(cells[row, 14]['v'], '12/15')
        self.assertIsNone(cells[row, 20]['v'])
        self.assertNotIn('2026-11-26', [cells[row, 11]['v'] for row in self.calendar_rows(sheet)])
        payment = next(row for row in self.calendar_rows(sheet) if cells[row, 11]['v'] == '2026-12-15')
        self.assertNotIn((payment, 14), cells)

    def test_missing_invalid_and_bare_month_day_dates_are_undetermined_not_inferred(self):
        for facts in ({}, {'dps': 0}, {'rights': '3/27・9/28', 'payments': '2026-09-32'},
                      {'rights': '2025-03-27', 'payments': '2027-06-20'},
                      {'no_dividend': True}, {'no_dividend': True, 'no_dividend_source': ''}):
            with self.subTest(facts=facts):
                self.facts({'9900': facts})
                cells = grid(self.build()); row = self.summary_row(cells)
                self.assertEqual([cells[row, column]['v'] for column in (13, 14, 15)], ['未定'] * 3)

    def test_explicit_sourced_zero_policy_is_no_dividend_and_known_rights_stay_visible(self):
        self.facts({'9900': {'no_dividend': True, 'no_dividend_source': 'Verified zero policy'}})
        cells = grid(self.build()); row = self.summary_row(cells)
        self.assertEqual([cells[row, column]['v'] for column in (13, 14, 15)], ['無配', '無配', '未定'])
        self.facts({'9900': {'rights': '2026-03-27', 'no_dividend': True,
                              'no_dividend_source': 'Verified zero policy'}})
        cells = grid(self.build())
        self.assertEqual(cells[row, 13]['v'], '3/27')
        self.assertEqual(cells[row, 14]['v'], '無配')

    def test_template_block_geometry_is_dynamic_with_one_blank_row_between_blocks(self):
        for offset in range(1, 9):
            self.activities.add_cash('A', '配当', '2026-09-15', str(offset),
                                     'Anonymous Security ' + str(offset), code=str(9900 + offset))
        self.activities.add_cash('B', '利息', '2024-01-01', '3')
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        self.assertEqual([blocks[key] for key in ('summary_last_row', 'cumulative_header_row',
                                                  'cumulative_last_row', 'calendar_label_row')],
                         [11, 13, 18, 20])
        self.assertEqual(cells[11, 11]['v'], '合計')
        self.assertNotIn((11, 12), cells)
        self.assertFalse(any((row, column) in cells for row in (12, 19) for column in range(11, 25)))
        self.assertEqual(cells[20, 11]['v'], 2026)
        self.assertEqual(cells[18, 12]['v'], 'SUM(L17:N17)')

    def test_configured_summary_order_changes_rows_without_changing_receipts(self):
        from dividend_order import save
        self.activities.add_cash('B', '配当', '2026-10-16', '500', 'Another Security', code='9911')
        before = self.activities.events()
        save(self.store.directory, ['9911', '9900'])
        sheet = self.build(); cells = grid(sheet)
        self.assertEqual((cells[2, 11]['v'], cells[3, 11]['v']), (9911, 9900))
        self.assertEqual(self.activities.events(), before)
        self.assertEqual(sum(cell['v'] for (row, column), cell in cells.items()
                             if column == 9 and row > 1 and cells[row, 6]['v'] == '配当'), 600)

    def test_normalized_order_preference_retains_original_unadapted_code_joins(self):
        import hashlib
        from dividend_order import save
        from ledger import Snapshot
        trade = dict(trade_date='2026-10-01', settlement_date='2026-10-05',
                     name='Alphabetic Security', code='314a', market='東証', side='買',
                     custody='NISA', tax_class='', quantity='10', price='100',
                     fees='0', tax='0', settlement_amount='1000')
        raw = json.dumps(trade).encode()
        self.store.import_snapshot('A', Snapshot(raw, 'alphabetic.csv', hashlib.sha256(raw).hexdigest(),
                                                 '2026-10-01', '2026-10-01', [trade]))
        save(self.store.directory, ['314A', '9900'])
        before = self.activities.events()
        cells = grid(self.build())
        self.assertEqual(cells[2, 11]['v'], '314a')
        self.assertIn('K2', cells[2, 22]['v'])
        self.assertEqual(cells[3, 11]['v'], 9900)
        self.assertEqual(self.activities.events(), before)

    def test_calendar_is_only_current_year_and_sorted_including_actual_payments(self):
        self.activities.add_cash('A', '配当', '2025-12-25', '9', 'Example Security', code='9900')
        self.facts({'9900': {'rights': '2025-12-26・2026-03-27・2027-03-29',
                              'payments': '2026-12-20・2027-06-20', 'earnings': '2026-01-29'}})
        sheet = self.build(); cells = grid(sheet)
        stamps = [cells[row, 11]['v'] for row in self.calendar_rows(sheet)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(stamp.startswith('2026-') for stamp in stamps))
        self.assertIn('2026-10-15', stamps)
        self.assertNotIn('2025-12-25', stamps)
        self.assertTrue(all(cells[row, 11]['format'] == 'm/d' for row in self.calendar_rows(sheet)))

    def test_all_events_in_inclusive_one_calendar_month_window_are_dynamic_red(self):
        self.facts({'9900': {'rights': '2026-10-19・2026-10-20・2026-11-20・2026-11-21',
                              'payments': '2026-10-20', 'earnings': '2026-11-01'}})
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        selected = [(cells[row, 11]['v'], cells[row, 13]['v']) for row in blocks['calendar_highlight_rows']]
        self.assertEqual(selected, [('2026-10-20', '権利付き最終日'), ('2026-10-20', '配当金支払日'),
                                    ('2026-11-01', '本決算発表日'), ('2026-11-20', '権利付き最終日')])
        self.assertEqual(len(sheet['conditional_formats']), 1)
        rule = sheet['conditional_formats'][0]
        self.assertEqual(rule['dxf'], 1)
        self.assertIn('<=EDATE(TODAY(),1)', rule['formula'])
        self.assertNotIn('COUNTIF', rule['formula'])
        self.assertEqual(rule['range'], f'K{blocks["calendar_first_row"]}:N{blocks["calendar_last_row"]}')
        self.assertTrue(all(cell.get('fill') is None for cell in sheet['cells']))

    def test_one_month_window_clamps_at_february_end_not_thirty_day_guess(self):
        self.activities.add_cash('A', '配当', '2026-01-15', '1', 'Example Security', code='9900')
        self.facts({'9900': {'rights': '2026-02-28・2026-03-01'}})
        sheet = self.build(today=date(2026, 1, 31)); cells = grid(sheet)
        stamps = [cells[row, 11]['v'] for row in sheet['blocks']['calendar_highlight_rows']]
        self.assertEqual(stamps, ['2026-02-28'])

    def test_calendar_payment_actuals_sum_accounts_and_other_event_amounts_are_blank(self):
        self.activities.add_cash('B', '配当', '2026-10-15', '50', 'Example Security', code='9900')
        self.facts({'9900': {'rights': '2026-10-15', 'payments': '2026-10-15', 'earnings': '2026-10-15'}})
        sheet = self.build(); cells = grid(sheet)
        for row in self.calendar_rows(sheet):
            if cells[row, 13]['v'] == '配当金支払日':
                self.assertEqual(cells[row, 14]['k'], 'formula_number')
                self.assertIn('SUMIFS', cells[row, 14]['v'])
                self.assertEqual(cells[row, 14]['format'], '#,##0')
            else:
                self.assertNotIn((row, 14), cells)

    def test_current_year_calendar_for_past_report_does_not_guess_forecast_from_past_year(self):
        self.facts({'9900': {'calendar_complete': True,
                              'events': [{'rights': '2027-03-26', 'payment': '2027-06-20', 'dps': 2}]}}, 2027)
        sheet = self.build(today=date(2027, 1, 10)); cells = grid(sheet)
        payment = next(row for row in self.calendar_rows(sheet) if cells[row, 13]['v'] == '配当金支払日')
        self.assertEqual(cells[payment, 11]['v'], '2027-06-20')
        self.assertNotIn((payment, 14), cells)

    def test_manual_incomes_cumulative_formulas_and_raw_evidence_are_unchanged(self):
        self.activities.add_cash('B', '税還付', '2026-10-19', '123', 'Opaque preserved description')
        self.activities.add_cash('B', '利息', '2022-12-20', '7')
        self.facts({'9900': {'rights': '2026-03-27', 'payments': '2026-06-20'}})
        before = '\n'.join(self.store.db.iterdump())
        research_before = (self.store.directory / 'market/research_2026.json').read_bytes()
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        for row in (row for row, column in cells if column == 6 and cells[row, column]['v'] in ('税還', '利息')):
            self.assertTrue(all(cells[row, column]['v'] is None for column in (3, 4, 5, 7, 8)))
        total = blocks['cumulative_total_row']
        for column in (12, 13, 14):
            self.assertEqual(cells[total, column]['k'], 'formula_number')
            self.assertTrue(cells[total, column]['v'].startswith('SUM('))
        years = [cells[row, 11]['v'] for row in range(blocks['cumulative_header_row'] + 1, total)]
        self.assertEqual(years, [2022, 2023, 2024, 2025, 2026])
        self.assertEqual('\n'.join(self.store.db.iterdump()), before)
        self.assertEqual((self.store.directory / 'market/research_2026.json').read_bytes(), research_before)


if __name__ == '__main__':
    unittest.main()
