"""Offline dated-price ingestion: public references never become cash evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import pandas as pd

from market_data import load_research, refresh_month_closes
from yahoo_market import month_closes


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def prices(**extra):
    result = dict(close=100.25, close_date='2026-09-30', price_source='Synthetic public previous close',
                  month_start_close=101.75, month_start_close_date='2026-10-01',
                  month_start_price_source='Synthetic public first-day close')
    result.update(extra)
    return result


class YahooMonthCloseTests(unittest.TestCase):
    def query(self, rows, month='2026-10', now=NOW, code='9900'):
        ticker = Mock()
        frame = pd.DataFrame({'Close': [value for _, value in rows]},
                             index=pd.to_datetime([day for day, _ in rows]))
        ticker.history.return_value = frame
        factory = Mock(return_value=ticker)
        result = month_closes(code, month, factory=factory, now=now)
        return result, factory, ticker

    def test_one_unadjusted_local_security_query_supplies_both_exact_dated_prices(self):
        result, factory, ticker = self.query([
            ('2026-09-29', 98), ('2026-09-30', 100.25),
            ('2026-10-01', 101.75), ('2026-10-02', 999)])
        self.assertEqual(result['close'], 100.25)
        self.assertEqual(result['close_date'], '2026-09-30')
        self.assertEqual(result['month_start_close'], 101.75)
        self.assertEqual(result['month_start_close_date'], '2026-10-01')
        factory.assert_called_once_with('9900.T')
        ticker.history.assert_called_once()
        options = ticker.history.call_args.kwargs
        self.assertEqual(options['start'], '2026-09-01')
        self.assertEqual(options['end'], '2026-10-02')
        self.assertFalse(options['auto_adjust'])
        self.assertFalse(options['actions'])
        # No currency quote, FX conversion or dividend lookup was requested.
        self.assertEqual([call[0] for call in ticker.method_calls], ['history'])

    def test_literal_first_day_holiday_is_not_replaced_by_the_next_trading_day(self):
        result, _, _ = self.query([('2026-10-30', 100), ('2026-11-02', 120)],
                                  month='2026-11', now=datetime(2026, 11, 3, 12, tzinfo=JST))
        self.assertEqual(result['close_date'], '2026-10-30')
        self.assertNotIn('month_start_close', result)
        self.assertNotIn('month_start_close_date', result)

    def test_today_first_day_bar_is_unfinished_before_16_jst(self):
        rows = [('2026-09-30', 100), ('2026-10-01', 120)]
        for now in (datetime(2026, 10, 1, 15, 59, 59, tzinfo=JST),
                    datetime(2026, 10, 1, 6, 59, tzinfo=timezone.utc)):
            with self.subTest(now=now):
                result, _, _ = self.query(rows, now=now)
                self.assertEqual(result['close'], 100)
                self.assertNotIn('month_start_close', result)
        ready, _, _ = self.query(rows, now=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(ready['month_start_close'], 120)

    def test_future_month_never_contacts_provider(self):
        factory = Mock(side_effect=AssertionError('A future close cannot be queried'))
        with self.assertRaises(ValueError):month_closes('9900', '2026-11', factory=factory, now=NOW)
        factory.assert_not_called()

    def test_old_previous_month_bar_outside_last_seven_days_is_not_a_month_end_price(self):
        result, _, _ = self.query([('2026-09-22', 100), ('2026-10-01', 120)])
        self.assertNotIn('close', result)
        self.assertEqual(result['month_start_close'], 120)

    def test_nonpositive_nonfinite_and_boolean_bars_do_not_become_closing_prices(self):
        for value in (0, -1, float('nan'), float('inf'), True, False):
            with self.subTest(value=value):
                result, _, _ = self.query([('2026-09-30', value), ('2026-10-01', value)])
                self.assertNotIn('close', result)
                self.assertNotIn('month_start_close', result)

    def test_latest_valid_prior_bar_is_selected_without_converting_currency(self):
        result, _, _ = self.query([('2026-09-28', 123.45), ('2026-09-30', float('nan'))])
        self.assertEqual(result['close'], 123.45)
        self.assertEqual(result['close_date'], '2026-09-28')

    def test_duplicate_first_day_bars_are_ambiguous_and_never_picked_arbitrarily(self):
        result, _, _ = self.query([('2026-09-30', 100), ('2026-10-01', 110), ('2026-10-01', 111)])
        self.assertEqual(result['close'], 100)
        self.assertNotIn('month_start_close', result)

    def test_non_domestic_symbol_and_naive_current_clock_fail_before_provider(self):
        factory = Mock(side_effect=AssertionError('No query'))
        for symbol in ('USDJPY=X', 'AAPL', '../9900', ''):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                month_closes(symbol, '2026-10', factory=factory, now=NOW)
        with self.assertRaises(ValueError):month_closes('9900', '2026-10', factory=factory, now=datetime(2026, 10, 29))
        factory.assert_not_called()


class PublicReferenceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'market/research_2026.json'
        self.seed({})

    def seed(self, facts, **metadata):
        self.path.parent.mkdir(exist_ok=True)
        value = dict(year=2026, securities=deepcopy(facts), **metadata)
        self.path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=True), encoding='utf-8')
        return value

    def refresh(self, provider, codes=('9900',), month='2026-10', **options):
        return refresh_month_closes(self.directory, codes, month, provider=provider, now=NOW, **options)

    def test_fully_dated_manual_prices_skip_query_and_preserve_original_bytes(self):
        manual = dict(prices(), name='User chosen name', dps=0,
                      events=[dict(rights='2026-09-28', payment='2026-12-01', dps=0)],
                      calendar_complete=True, source='User verified source')
        self.seed({'9900': manual}, note='Preserve custom metadata')
        before = self.path.read_bytes()
        provider = Mock(side_effect=AssertionError('Known observations must not be queried again'))
        result = self.refresh(provider)
        self.assertEqual(result, dict(updated=0, failed=[], written=False))
        self.assertEqual(self.path.read_bytes(), before)
        provider.assert_not_called()

    def test_missing_first_day_is_filled_without_overwriting_manual_previous_or_dividend_plan(self):
        manual = dict(close=99.5, close_date='2026-09-30', price_source='Manually verified previous',
                      name='Custom name', dps=0, events=[dict(payment='2026-12-01', dps=0)],
                      source='Dividend source', status='Verified plan', calendar_complete=True)
        self.seed({'9900': manual}, note='keep')
        supplied = prices(close=999, price_source='Must not replace manual value')
        before_provider = deepcopy(supplied)
        provider = Mock(return_value=supplied)
        result = self.refresh(provider)
        self.assertTrue(result['written']); self.assertEqual(result['updated'], 1)
        current = json.loads(self.path.read_text(encoding='utf-8'))
        for key, value in manual.items():self.assertEqual(current['securities']['9900'][key], value)
        self.assertEqual(current['securities']['9900']['month_start_close'], 101.75)
        self.assertEqual(current['note'], 'keep')
        self.assertEqual(supplied, before_provider)
        provider.assert_called_once_with('9900', '2026-10')

    def test_stale_price_dates_can_be_replaced_but_unrelated_manual_facts_survive(self):
        manual = dict(close=10, close_date='2026-08-31', month_start_close=11,
                      month_start_close_date='2026-09-01', source='Existing source', name='Existing name',
                      dps=25, events=[dict(rights='2026-09-28', payment='2026-12-01', dps=25)])
        self.seed({'9900': manual})
        result = self.refresh(Mock(return_value=prices()))
        self.assertEqual(result['updated'], 1)
        current = load_research(self.directory, 2026)['securities']['9900']
        self.assertEqual(current['close'], 100.25)
        self.assertEqual(current['close_date'], '2026-09-30')
        for key in ('source', 'name', 'dps', 'events'):self.assertEqual(current[key], manual[key])

    def test_invalid_provider_prices_or_dates_do_not_overwrite_stale_cache(self):
        old = dict(close=10, close_date='2026-08-31', month_start_close=11, month_start_close_date='2026-09-01')
        self.seed({'9900': old}); before = self.path.read_bytes()
        for invalid in (0, -1, float('nan'), float('inf'), True, False):
            with self.subTest(invalid=invalid):
                provider = Mock(return_value=prices(close=invalid, month_start_close=invalid))
                result = self.refresh(provider)
                self.assertEqual(result['updated'], 0); self.assertFalse(result['written'])
                self.assertEqual(result['failed'], ['9900'])
                self.assertEqual(self.path.read_bytes(), before)
        result = self.refresh(Mock(return_value=prices(close_date='2026-09-01', month_start_close_date='2026-10-02')))
        self.assertFalse(result['written']); self.assertEqual(self.path.read_bytes(), before)

    def test_one_provider_failure_does_not_discard_another_valid_security(self):
        def provider(code, month):
            if code == '9901':raise OSError('Synthetic offline data provider')
            return prices()
        result = self.refresh(provider, codes=['9900', '9900', '9901', 'USDJPY=X', 'F-UNKNOWN'])
        self.assertEqual(result['updated'], 1); self.assertEqual(result['failed'], ['9901'])
        self.assertEqual(set(load_research(self.directory, 2026)['securities']), {'9900'})

    def test_first_day_before_16_accepts_previous_close_but_rejects_provider_intraday_bar(self):
        result = refresh_month_closes(self.directory, ['9900'], '2026-10',
            provider=Mock(return_value=prices()), now=datetime(2026, 10, 1, 15, 59, tzinfo=JST))
        self.assertEqual(result['updated'], 1)
        fact = load_research(self.directory, 2026)['securities']['9900']
        self.assertEqual(fact['close'], 100.25)
        self.assertNotIn('month_start_close', fact)

    def test_first_day_before_16_with_known_previous_price_needs_no_provider(self):
        self.seed({'9900': dict(close=100, close_date='2026-09-30')})
        before = self.path.read_bytes(); provider = Mock(side_effect=AssertionError('No finished first-day bar exists yet'))
        result = refresh_month_closes(self.directory, ['9900'], '2026-10', provider=provider,
                                      now=datetime(2026, 10, 1, 15, 59, tzinfo=JST))
        self.assertFalse(result['written']); provider.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_future_month_does_not_query_or_create_a_cache_file(self):
        provider = Mock(side_effect=AssertionError('No future query'))
        before = self.path.read_bytes()
        result = self.refresh(provider, month='2026-11')
        self.assertEqual(result, dict(updated=0, failed=['9900'], written=False))
        provider.assert_not_called(); self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / 'research_2027.json').exists())

    def test_cancelled_before_submission_never_starts_provider_or_changes_cache(self):
        provider = Mock(side_effect=AssertionError('Cancelled collection must not start'))
        before = self.path.read_bytes()
        result = self.refresh(provider, cancelled=lambda: True)
        self.assertTrue(result['cancelled']); self.assertFalse(result['written'])
        provider.assert_not_called(); self.assertEqual(self.path.read_bytes(), before)

    def test_cancellation_after_a_result_prevents_atomic_cache_write(self):
        before = self.path.read_bytes(); state = dict(cancelled=False)
        def provider(code, month):
            state['cancelled'] = True
            return prices()
        result = self.refresh(provider, cancelled=lambda: state['cancelled'])
        self.assertTrue(result['cancelled']); self.assertFalse(result['written'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_timeout_is_bounded_and_late_provider_completion_cannot_write_cache(self):
        release = threading.Event(); started = threading.Event(); completed = threading.Event(); before = self.path.read_bytes()
        def provider(code, month):
            try:
                started.set()
                release.wait(.6)
                return prices()
            finally:completed.set()
        start = time.monotonic()
        try:
            result = self.refresh(provider, budget_seconds=.04)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, .4)
            self.assertFalse(result['written']); self.assertEqual(result['failed'], ['9900'])
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            release.set()
            if started.is_set():self.assertTrue(completed.wait(1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_partial_timeout_keeps_finished_security_and_preserves_pending_security(self):
        self.seed({'9901': dict(name='Preserved pending', source='Manual source')})
        release = threading.Event(); started = threading.Event(); completed = threading.Event()
        def provider(code, month):
            if code == '9900':return prices()
            try:
                started.set()
                release.wait(.6)
                return prices(close=999)
            finally:completed.set()
        try:
            result = self.refresh(provider, codes=['9900', '9901'], budget_seconds=.08)
            self.assertTrue(result['written']); self.assertEqual(result['updated'], 1)
            self.assertEqual(result['failed'], ['9901'])
            saved = self.path.read_bytes()
            fact = load_research(self.directory, 2026)['securities']['9901']
            self.assertEqual(fact, dict(name='Preserved pending', source='Manual source'))
        finally:
            release.set()
            if started.is_set():self.assertTrue(completed.wait(1))
        self.assertEqual(self.path.read_bytes(), saved)


class PublicReferenceLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'market/research_2026.json'; self.path.parent.mkdir()

    def test_corrupt_or_duplicate_or_wrong_schema_file_is_preserved_as_an_empty_reference(self):
        examples = (b'{broken', b'{"year":2026,"year":2026,"securities":{}}',
                    b'{"year":true,"securities":{}}', b'{"year":2025,"securities":{}}',
                    b'{"year":2026,"securities":[]}', b'{"year":2026,"securities":{"9900":[]}}')
        for raw in examples:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertEqual(load_research(self.directory, 2026),
                                 dict(year=2026, securities={}, _load_error=True))
                provider = Mock(side_effect=AssertionError('Do not replace a broken manual reference'))
                result = refresh_month_closes(self.directory, ['9900'], '2026-10', provider=provider, now=NOW)
                self.assertFalse(result['written']); provider.assert_not_called()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_nested_malformed_public_fact_is_not_allowed_to_break_the_financial_report(self):
        examples = (dict(events=None), dict(events=['invalid event'], calendar_complete=True),
                    dict(dps={'not': 'a number'}), dict(dps=float('nan')))
        for fact in examples:
            with self.subTest(fact=fact):
                raw = json.dumps(dict(year=2026, securities={'9900': fact}), allow_nan=True).encode()
                self.path.write_bytes(raw)
                result = load_research(self.directory, 2026)
                self.assertEqual(result, dict(year=2026, securities={}, _load_error=True))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_internal_load_error_flag_is_not_trusted_from_an_otherwise_valid_manual_file(self):
        raw = json.dumps(dict(year=2026, securities={'9900': dict(dps=0, events=[])}, _load_error=True)).encode()
        self.path.write_bytes(raw)
        result = load_research(self.directory, 2026)
        self.assertNotIn('_load_error', result)
        self.assertEqual(result['securities']['9900']['dps'], 0)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_bad_public_fact_does_not_discard_valid_financial_rows_or_rewrite_the_ledger(self):
        from datetime import date
        from report_v36 import build
        from test_data_compatibility import seed_fixture
        store, activities = seed_fixture(self.directory)
        try:
            path = store.directory / 'market/research_2026.json'
            raw = b'{"year":2026,"securities":{"9900":{"events":null,"dps":{"bad":1}}}}'
            path.write_bytes(raw)
            before = '\n'.join(store.db.iterdump())
            records = store.records(); events = activities.events()
            model = build(store, activities, '2026-10', today=date(2026, 10, 29))
            transaction = {(cell['r'], cell['c']): cell['v'] for cell in model['sheets'][0]['cells']}
            self.assertEqual(transaction[2, 1], '2026-10-02')
            self.assertEqual(transaction[3, 1], '2026-09-01')
            receipts = [cell['v'] for cell in model['sheets'][1]['cells']
                        if cell['c'] == 9 and cell['r'] in (2, 3)]
            self.assertEqual(sorted(receipts), [12, 100])
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(store.records(), records); self.assertEqual(activities.events(), events)
            self.assertEqual('\n'.join(store.db.iterdump()), before)
        finally:store.close()

    def test_symlink_reference_is_not_followed_or_replaced(self):
        other = self.directory / 'outside.json'
        raw = b'{"year":2026,"securities":{"9900":{"dps":5}}}'
        other.write_bytes(raw)
        try:self.path.symlink_to(other)
        except (OSError, NotImplementedError):self.skipTest('Symlinks are unavailable')
        self.assertTrue(load_research(self.directory, 2026)['_load_error'])
        provider = Mock(side_effect=AssertionError('Do not overwrite a linked file'))
        result = refresh_month_closes(self.directory, ['9900'], '2026-10', provider=provider, now=NOW)
        self.assertFalse(result['written']); provider.assert_not_called()
        self.assertTrue(self.path.is_symlink()); self.assertEqual(other.read_bytes(), raw)


if __name__ == '__main__':unittest.main()
