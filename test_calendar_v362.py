"""Shared calendar and first-trading-day presentation, using anonymous data."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dividend_report import build_dividend_sheet
from ledger import Snapshot
from test_data_compatibility import seed_fixture
from upcoming_calendar import schedule_rows


JST = timezone(timedelta(hours=9))
TODAY = date(2026, 10, 31)


def grid(sheet):
    return {(cell['r'], cell['c']): cell for cell in sheet['cells']}


class CalendarV362(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store, self.activities = seed_fixture(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.facts({'9900': dict(rights='2026-11-25', payments='2026-12-15', earnings='2026-11-10',
                                  calendar_complete=True,
                                  events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})

    def facts(self, value, year=2026):
        (self.store.directory / f'market/research_{year}.json').write_text(
            json.dumps(dict(year=year, securities=value)), encoding='utf-8')

    def trade(self, *, account='A', ident='9900', side='売', quantity='75',
              day='2026-10-20', custody='NISA', **extra):
        record = dict(trade_date=day, settlement_date=day, name='Anonymous Security', code=ident,
                      market='東証', side=side, custody=custody, tax_class='', quantity=quantity,
                      price='100', fees='0', tax='0', settlement_amount=str(Decimal(quantity) * 100), **extra)
        raw = json.dumps(record, sort_keys=True).encode()
        self.store.import_snapshot(account, Snapshot(raw, 'anonymous-extra.csv', hashlib.sha256(raw).hexdigest(),
                                                      day, day, [record]))

    def build(self, as_of='2026-10-31', today=TODAY):
        return build_dividend_sheet(self.store, self.activities, as_of, today=today)

    def summary_row(self, cells, ident=9900):
        return next(row for row, column in cells if column == 11 and cells[row, column]['v'] == ident)

    def test_existing_held_security_keeps_past_events_and_upcoming_filter_removes_them(self):
        all_rows = schedule_rows(self.store, self.activities, today=TODAY)
        upcoming = schedule_rows(self.store, self.activities, today=TODAY, upcoming_only=True)
        self.assertIn('2026-10-15', [row['date'] for row in all_rows])
        self.assertNotIn('2026-10-15', [row['date'] for row in upcoming])
        self.assertTrue(all(row['date'] >= TODAY.isoformat() for row in upcoming))
        self.assertTrue(all(row['held_quantity'] == Decimal(75) for row in all_rows))

    def test_sold_out_security_disappears_from_every_calendar_but_its_income_stays(self):
        self.trade()
        self.assertEqual(schedule_rows(self.store, self.activities, today=TODAY), [])
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        self.assertLess(blocks['calendar_last_row'], blocks['calendar_first_row'])
        receipt = next(row for row, column in cells if column == 6 and cells[row, column]['v'] == '配当')
        self.assertEqual(cells[receipt, 9]['v'], 100)
        summary = self.summary_row(cells)
        self.assertIn('SUMIFS', cells[summary, 22]['v'])
        self.assertTrue(cells[blocks['cumulative_total_row'], 12]['v'].startswith('SUM('))

    def test_remaining_shares_in_another_account_keep_the_security_and_all_actual_receipts(self):
        self.trade()
        self.trade(account='B', side='買', quantity='50', day='2026-09-05')
        self.activities.add_cash('B', '配当', '2026-10-15', '50', 'Example Security', code='9900', custody='NISA')
        rows = schedule_rows(self.store, self.activities, today=TODAY)
        self.assertTrue(all(row['held_quantity'] == Decimal(50) for row in rows))
        actual = next(row for row in rows if row['date'] == '2026-10-15')
        self.assertEqual(actual['amount'], Decimal(150))

    def test_multiple_custodies_are_totalled_without_netting_unknown_accounts_to_zero(self):
        self.trade(custody='特定', side='買', quantity='25', day='2026-09-05')
        rows = schedule_rows(self.store, self.activities, today=TODAY)
        self.assertTrue(all(row['held_quantity'] == Decimal(100) for row in rows))
        # An unsupported opening shortfall cannot cancel the known 100 shares.
        self.trade(account='B', quantity='100', day='2026-10-21')
        rows = schedule_rows(self.store, self.activities, today=TODAY)
        self.assertTrue(rows)
        self.assertTrue(all(row['held_quantity'] is None for row in rows))
        self.assertIsNone(next(row for row in rows if row['date'] == '2026-12-15')['amount'])

    def test_manual_split_numeric_strings_are_counted_before_full_sale(self):
        from split_entry import register
        split = register(self.store, self.activities, 'A', '9900', '2026-10-10', 1, 2, 'Anonymous split')
        self.assertIsInstance(split['added'], str)
        self.assertEqual(schedule_rows(self.store, self.activities, today=TODAY)[0]['held_quantity'], Decimal(150))
        self.trade(quantity='150')
        self.assertEqual(schedule_rows(self.store, self.activities, today=TODAY), [])

    def test_unknown_history_is_not_hidden_and_cannot_produce_a_forecast_zero(self):
        self.trade(ident='9911', quantity='10')
        self.facts({'9911': dict(calendar_complete=True, earnings='2026-11-10',
                                  events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})
        rows = [row for row in schedule_rows(self.store, self.activities, today=TODAY) if row['code'] == '9911']
        self.assertTrue(rows)
        self.assertTrue(all(row['held_quantity'] is None for row in rows))
        self.assertIsNone(next(row for row in rows if row['kind'] == '配当金支払日')['amount'])

    def test_split_notice_without_opening_history_stays_unknown_instead_of_becoming_zero(self):
        from activity import key
        event = dict(type='株式分割', date='2026-10-10', code='9911', name='Anonymous split-only security',
                     before='10', added='10', after='20', currency='JPY')
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO activities(account,kind,event_key,event_date,payload,source) VALUES (?,?,?,?,?,?)',
                ('A', event['type'], key(event), event['date'], json.dumps(event), 'Anonymous fixture'))
        self.facts({'9911': dict(earnings='2026-11-10')})
        rows = [row for row in schedule_rows(self.store, self.activities, today=TODAY) if row['code'] == '9911']
        self.assertTrue(rows)
        self.assertIsNone(rows[0]['held_quantity'])

    def test_future_purchase_is_not_a_current_holding(self):
        self.trade(ident='9911', side='買', quantity='10', day='2026-11-05')
        self.facts({'9911': dict(earnings='2026-11-10')})
        self.assertNotIn('9911', [row['code'] for row in schedule_rows(self.store, self.activities, today=TODAY)])

    def test_future_sale_does_not_remove_currently_held_security(self):
        self.trade(day='2026-11-05')
        rows = schedule_rows(self.store, self.activities, today=TODAY)
        self.assertTrue(rows)
        self.assertTrue(all(row['held_quantity'] == Decimal(75) for row in rows))

    def test_raw_code_aliases_share_grouped_holding_and_user_display_name(self):
        from security_names import save
        self.trade(ident='314a', side='買', quantity='10', day='2026-09-05')
        self.trade(ident='314A', quantity='10')
        save(self.store.directory, '314A', 'Short public name')
        self.facts({'314A': dict(earnings='2026-11-10')})
        self.assertNotIn('314A', [row['code'] for row in schedule_rows(self.store, self.activities, today=TODAY)])
        self.trade(account='B', ident='314A', side='買', quantity='5', day='2026-09-05')
        rows = [row for row in schedule_rows(self.store, self.activities, today=TODAY) if row['code'] == '314A']
        self.assertTrue(rows)
        self.assertEqual(rows[0]['name'], 'Short public name')
        self.assertEqual(rows[0]['held_quantity'], Decimal(5))

    def test_fund_quote_units_never_become_per_share_calendar_forecasts(self):
        self.trade(ident='9903', side='買', quantity='10000', day='2026-09-05', asset_type='投資信託',
                   quantity_unit='口', price_unit='円/1万口')
        self.facts({'9903': dict(calendar_complete=True,
                                  events=[dict(rights='2026-11-25', payment='2026-12-15', dps=2)])})
        payment = next(row for row in schedule_rows(self.store, self.activities, today=TODAY)
                       if row['kind'] == '配当金支払日' and row['code'] == '9903')
        self.assertIsNone(payment['amount'])
        self.assertEqual(payment['held_quantity'], Decimal(10000))

    def test_projection_is_current_year_sorted_and_one_month_highlights_include_all_ties(self):
        self.facts({'9900': dict(rights='2025-12-26・2026-10-31・2026-11-30・2026-12-01',
                                  payments='2026-10-31・2027-01-05', earnings='2026-10-31')})
        rows = schedule_rows(self.store, self.activities, today=TODAY, upcoming_only=True)
        self.assertEqual([row['date'] for row in rows], sorted(row['date'] for row in rows))
        self.assertTrue(all(row['date'].startswith('2026-') for row in rows))
        self.assertEqual(sum(row['within_month'] for row in rows), 4)
        self.assertFalse(next(row for row in rows if row['date'] == '2026-12-01')['within_month'])

    def test_calendar_amounts_are_numeric_only_and_excel_retains_actual_sum_formulas(self):
        rows = schedule_rows(self.store, self.activities, today=TODAY)
        actual = next(row for row in rows if row['date'] == '2026-10-15')
        forecast = next(row for row in rows if row['date'] == '2026-12-15')
        self.assertEqual((actual['amount'], forecast['amount']), (Decimal(100), Decimal(150)))
        sheet = self.build(); cells = grid(sheet); blocks = sheet['blocks']
        for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1):
            if (row, 14) in cells:
                self.assertEqual(cells[row, 14]['format'], '#,##0')
                self.assertNotIn('実績', str(cells[row, 14]['v']))
                self.assertNotIn('予想', str(cells[row, 14]['v']))
        actual_row = next(row for row in range(blocks['calendar_first_row'], blocks['calendar_last_row'] + 1)
                          if cells[row, 11]['v'] == '2026-10-15')
        self.assertEqual(cells[actual_row, 14]['k'], 'formula_number')
        self.assertIn('SUMIFS', cells[actual_row, 14]['v'])

    def test_current_hold_filter_is_independent_of_historical_report_cutoff(self):
        self.trade()
        self.assertEqual(schedule_rows(self.store, self.activities, today=TODAY, as_of='2026-09-30'), [])
        sheet = self.build(as_of='2026-09-30')
        self.assertLess(sheet['blocks']['calendar_last_row'], sheet['blocks']['calendar_first_row'])

    def test_projection_does_not_change_database_research_or_order_files(self):
        from dividend_order import save
        save(self.store.directory, ['9900'])
        before_db = '\n'.join(self.store.db.iterdump())
        paths = [self.store.directory / 'market/research_2026.json', self.store.directory / 'dividend_order.json']
        before_files = {path: path.read_bytes() for path in paths}
        schedule_rows(self.store, self.activities, today=TODAY)
        self.build()
        self.assertEqual('\n'.join(self.store.db.iterdump()), before_db)
        self.assertEqual({path: path.read_bytes() for path in paths}, before_files)

    def test_first_trading_day_price_accepts_november_second_and_records_actual_date_in_note(self):
        self.facts({'9900': dict(close=130, close_date='2026-10-30', price_source='Public historical source',
                                  month_open_close=132, month_open_close_date='2026-11-02',
                                  month_open_price_source='Public first trading day source')})
        cells = grid(self.build('2026-11-30', today=date(2026, 11, 7))); row = self.summary_row(cells)
        self.assertEqual(cells[1, 24]['v'], '月初終値')
        self.assertEqual(cells[row, 24]['v'], 132)
        self.assertIn('2026-11-02', cells[row, 24]['comment'])
        self.assertIn('Public first trading day source', cells[row, 24]['comment'])
        self.assertIn('2026-10-30', cells[row, 23]['comment'])

    def test_legacy_literal_weekend_first_and_later_observed_bar_cannot_replace_opening_day(self):
        for facts in (dict(month_start_close=132, month_start_close_date='2026-11-01'),
                      dict(month_open_close=132, month_open_close_date='2026-11-03')):
            self.facts({'9900': facts})
            cells = grid(self.build('2026-11-30', today=date(2026, 11, 7))); row = self.summary_row(cells)
            self.assertIsNone(cells[row, 24]['v'])
            self.assertNotIn('comment', cells[row, 24])

    def test_legacy_literal_first_is_compatible_only_when_calendar_confirms_it_traded(self):
        self.facts({'9900': dict(month_start_close=132, month_start_close_date='2026-10-01',
                                  month_start_price_source='Older dated source')})
        cells = grid(self.build()); row = self.summary_row(cells)
        self.assertEqual(cells[row, 24]['v'], 132)
        self.assertIn('2026-10-01', cells[row, 24]['comment'])
        self.assertIn('Older dated source', cells[row, 24]['comment'])

    def test_future_or_unfinished_opening_day_price_remains_blank(self):
        self.facts({'9900': dict(month_open_close=132, month_open_close_date='2026-11-02',
                                  month_open_price_source='Verified fixture source')})
        cells = grid(self.build('2026-11-30', today=date(2026, 11, 1))); row = self.summary_row(cells)
        self.assertIsNone(cells[row, 24]['v'])
        for hour, minute, expected in ((15, 59, None), (16, 0, 132)):
            cells = grid(self.build('2026-11-30', today=datetime(2026, 11, 2, hour, minute, tzinfo=JST)))
            self.assertEqual(cells[row, 24]['v'], expected)

    def test_selected_historical_month_and_geometry_remain_unchanged(self):
        self.facts({'9900': dict(month_open_close=132, month_open_close_date='2026-09-01',
                                  month_open_price_source='Verified fixture source')})
        sheet = self.build('2026-09-30', today=TODAY); cells = grid(sheet); row = self.summary_row(cells)
        self.assertEqual(cells[row, 24]['v'], 132)
        self.assertEqual((sheet['row_height'], sheet['header_height']), (24, 44.1))
        self.assertEqual((sheet['widths']['11'], sheet['widths']['12'], sheet['widths']['24']), (13, 25, 16))
        self.assertEqual(sheet['blocks']['cumulative_header_row'], sheet['blocks']['summary_last_row'] + 2)
        self.assertEqual(sheet['blocks']['calendar_label_row'], sheet['blocks']['cumulative_last_row'] + 2)

    def test_opening_price_requires_its_own_nonempty_source_for_new_and_legacy_fields(self):
        for facts in (dict(month_open_close=132, month_open_close_date='2026-11-02'),
                      dict(month_open_close=132, month_open_close_date='2026-11-02', month_open_price_source=' '),
                      dict(month_start_close=132, month_start_close_date='2026-10-01'),
                      dict(month_start_close=132, month_start_close_date='2026-10-01', month_start_price_source=' ')):
            self.facts({'9900': facts})
            month = '2026-11-30' if 'month_open_close' in facts else '2026-10-31'
            cells = grid(self.build(month, today=date(2026, 11, 7))); row = self.summary_row(cells)
            self.assertIsNone(cells[row, 24]['v'])
            self.assertNotIn('comment', cells[row, 24])

    def test_default_report_clock_reaches_renderer_and_blocks_unfinished_opening_bar(self):
        from unittest.mock import patch
        import report_v36
        self.facts({'9900': dict(month_open_close=132, month_open_close_date='2026-11-02',
                                  month_open_price_source='Synthetic verified source')})
        for hour, minute, expected in ((15, 59, None), (16, 0, 132)):
            class FixedClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 11, 2, hour, minute, tzinfo=JST).astimezone(tz or JST)
            with patch.object(report_v36, 'datetime', FixedClock):
                model = report_v36.build(self.store, self.activities)
            self.assertEqual(model['as_of'], '2026-11-02')
            cells = grid(next(sheet for sheet in model['sheets'] if sheet['name'] == '配当'))
            row = self.summary_row(cells)
            self.assertEqual(cells[row, 24]['v'], expected)


if __name__ == '__main__':
    unittest.main()
