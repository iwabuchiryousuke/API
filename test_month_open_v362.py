"""First JPX trading-day prices, including weekends, holidays and UTC bars."""
from datetime import date,datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd

from jpx_calendar import (first_trading_day,opening_day,is_trading_day,
                          national_holidays,CALENDAR_SOURCE)
from market_data import load_research,refresh_month_closes,refresh_public_research
from public_research import _day,yahoo_japan_month_closes
from yahoo_market import month_closes
from test_public_research_v361 import Response,yahoo_html,reference

JST=timezone(timedelta(hours=9))
NOV=datetime(2026,11,7,12,tzinfo=JST)


def opening_prices(**extra):
    value=dict(close=100,close_date='2026-10-30',price_source='Manual previous observation',
               month_open_close=120,month_open_close_date='2026-11-02',
               month_open_price_source='Synthetic unadjusted opening-day quote')
    value.update(extra);return value


class JapaneseExchangeCalendar(unittest.TestCase):
    def test_weekend_month_first_is_followed_to_the_expected_monday(self):
        self.assertEqual(first_trading_day('2026-11'),date(2026,11,2))
        self.assertEqual(opening_day(2026,11),date(2026,11,2))
        self.assertFalse(is_trading_day(date(2026,11,1)))
        self.assertTrue(is_trading_day(date(2026,11,2)))

    def test_new_year_exchange_closures_and_golden_week_are_included(self):
        for month,expected in (('2025-01',date(2025,1,6)),('2026-01',date(2026,1,5)),
                               ('2027-01',date(2027,1,4)),('2027-05',date(2027,5,6))):
            self.assertEqual(first_trading_day(month),expected)
        self.assertFalse(is_trading_day(date(2026,1,2)))
        self.assertFalse(is_trading_day(date(2026,12,31)))

    def test_citizen_substitute_equinox_and_happy_monday_holidays(self):
        holidays=national_holidays(2026)
        for day in (date(2026,9,22),date(2026,5,6),date(2026,3,20),
                    date(2026,9,23),date(2026,1,12),date(2026,7,20),date(2026,10,12)):
            self.assertIn(day,holidays);self.assertFalse(is_trading_day(day))
        self.assertIn(date(2027,3,22),national_holidays(2027))
        self.assertIn(date(2025,2,24),national_holidays(2025))

    def test_ordinary_business_day_one_is_not_changed(self):
        self.assertEqual(first_trading_day('2026-10'),date(2026,10,1))
        self.assertEqual(first_trading_day('2026-05'),date(2026,5,1))

    def test_year_and_date_typing_fail_closed_outside_supported_statutory_period(self):
        for invalid in ('2024-11','2100-01','2026-1','2026-13',True,None):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):first_trading_day(invalid)
        for day in (datetime(2026,1,1),True,'2026-11-02'):
            with self.subTest(day=day),self.assertRaises(ValueError):is_trading_day(day)


class YahooOpeningQueries(unittest.TestCase):
    def query(self,rows,month='2026-11',now=NOV):
        ticker=Mock();ticker.history.return_value=pd.DataFrame({'Close':[value for stamp,value in rows]},
                            index=pd.to_datetime([stamp for stamp,value in rows]))
        result=month_closes('9900',month,factory=lambda code:ticker,now=now)
        return result,ticker

    def test_november_open_is_november_two_and_literal_one_remains_absent(self):
        result,ticker=self.query([('2026-10-30',100),('2026-11-02',120),('2026-11-04',999)])
        self.assertEqual(result['close'],100);self.assertEqual(result['month_open_close'],120)
        self.assertEqual(result['month_open_close_date'],'2026-11-02')
        self.assertNotIn('month_start_close',result)
        self.assertEqual(ticker.history.call_args.kwargs['end'],'2026-11-03')
        self.assertFalse(ticker.history.call_args.kwargs['auto_adjust'])

    def test_utc_daily_midnight_is_normalized_to_jst_before_selecting_both_prices(self):
        result,_=self.query([('2026-09-29T15:00:00Z',100),('2026-09-30T15:00:00Z',120)],
                            month='2026-10',now=datetime(2026,10,5,12,tzinfo=JST))
        self.assertEqual(result['close'],100);self.assertEqual(result['close_date'],'2026-09-30')
        self.assertEqual(result['month_start_close'],120)
        self.assertEqual(result['month_open_close'],120);self.assertEqual(result['month_open_close_date'],'2026-10-01')

    def test_golden_week_queries_exact_expected_day_instead_of_first_returned_bar(self):
        now=datetime(2027,5,10,12,tzinfo=JST)
        result,ticker=self.query([('2027-04-30',100),('2027-05-06',120),('2027-05-07',999)],'2027-05',now)
        self.assertEqual(result['month_open_close_date'],'2027-05-06')
        self.assertEqual(ticker.history.call_args.kwargs['end'],'2027-05-07')
        missing,_=self.query([('2027-04-30',100),('2027-05-07',999)],'2027-05',now)
        self.assertNotIn('month_open_close',missing)

    def test_expected_missing_bar_and_conflicting_duplicate_open_remain_absent(self):
        for rows in ([('2026-10-30',100),('2026-11-04',999)],
                     [('2026-10-30',100),('2026-11-02',120),('2026-11-02',121)]):
            result,_=self.query(rows);self.assertNotIn('month_open_close',result)

    def test_today_unfinished_opening_price_is_not_published_before_16_jst(self):
        rows=[('2026-10-30',100),('2026-11-02',120)]
        for now in (datetime(2026,11,2,15,59,tzinfo=JST),datetime(2026,11,2,6,59,tzinfo=timezone.utc),
                    datetime(2026,11,1,17,tzinfo=JST)):
            result,_=self.query(rows,now=now);self.assertNotIn('month_open_close',result)
        result,_=self.query(rows,now=datetime(2026,11,2,7,tzinfo=timezone.utc))
        self.assertEqual(result['month_open_close'],120)

    def test_new_year_query_includes_exchange_first_session_and_never_calls_jan_two_open(self):
        result,ticker=self.query([('2025-12-30',100),('2026-01-05',120)],'2026-01',datetime(2026,1,10,tzinfo=JST))
        self.assertEqual(result['month_open_close_date'],'2026-01-05')
        self.assertEqual(ticker.history.call_args.kwargs['end'],'2026-01-06')
        self.assertNotIn('month_start_close',result)

    def test_japanese_provider_complete_open_skips_chart_and_partial_provider_falls_back(self):
        supplied=opening_prices()
        with patch('public_research.yahoo_japan_month_closes',return_value=supplied),patch('yfinance.Ticker') as ticker:
            self.assertEqual(month_closes('9900','2026-11',now=NOV),supplied);ticker.assert_not_called()
        ticker=Mock();ticker.history.return_value=pd.DataFrame({'Close':[120]},index=pd.to_datetime(['2026-11-02']))
        previous=dict(close=100,close_date='2026-10-30',price_source='Japanese historical')
        with patch('public_research.yahoo_japan_month_closes',return_value=previous),patch('yfinance.Ticker',return_value=ticker):
            result=month_closes('9900','2026-11',now=NOV)
        self.assertEqual(result['close'],100);self.assertEqual(result['price_source'],'Japanese historical')
        self.assertEqual(result['month_open_close'],120)

    def test_conflicting_previous_daily_bars_are_not_picked_by_largest_amount(self):
        result,_=self.query([('2026-10-30',100),('2026-10-30',999),('2026-11-02',120)])
        self.assertNotIn('close',result);self.assertEqual(result['month_open_close'],120)


class YahooJapanOpeningPage(unittest.TestCase):
    def test_page_query_includes_opening_day_and_preserves_actual_quote_date(self):
        text=yahoo_html([('2026/10/30',1,2,0,100,999,99),('2026/11/2',1,2,0,120,999,119)])
        opener=Mock(side_effect=lambda req,**kw:Response(text,req.full_url))
        result=yahoo_japan_month_closes('9900','2026-11',opener=opener,now=NOV)
        self.assertEqual(result['month_open_close'],120);self.assertEqual(result['month_open_close_date'],'2026-11-02')
        self.assertIn('to=20261103',opener.call_args.args[0].full_url)
        self.assertNotIn('month_start_close',result)
        self.assertEqual(result['month_open_price_source'],result['price_source'])

    def test_later_page_quote_does_not_replace_the_missing_expected_opening_quote(self):
        text=yahoo_html([('2026/10/30',1,2,0,100,999,99),('2026/11/4',1,2,0,999,999,119)])
        result=yahoo_japan_month_closes('9900','2026-11',opener=lambda req,**kw:Response(text,req.full_url),now=NOV)
        self.assertNotIn('month_open_close',result)

    def test_public_date_variants_keep_the_tokyo_trading_date(self):
        self.assertEqual(_day('2026年10月1日（木）'),date(2026,10,1))
        self.assertEqual(_day('2026-10-01 00:00:00'),date(2026,10,1))
        self.assertEqual(_day('2026-09-30T15:00:00Z'),date(2026,10,1))


class MonthOpeningCache(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.folder=self.base/'market';self.folder.mkdir()
        self.path=self.folder/'research_2026.json';self.seed({})
    def seed(self,facts):self.path.write_text(json.dumps(dict(year=2026,securities=facts)),encoding='utf-8')
    def refresh(self,provider,month='2026-11',now=NOV):
        return refresh_month_closes(self.base,['9900'],month,provider=provider,now=now,include_month_open=True)

    def test_opening_cache_stores_actual_date_and_calendar_provenance_preserving_previous_manual(self):
        manual=dict(close=99,close_date='2026-10-30',price_source='Manual previous',source='Manual dividend',dps=5,events=[])
        self.seed({'9900':manual});result=self.refresh(Mock(return_value=opening_prices()))
        self.assertEqual(result['failed'],[])
        fact=load_research(self.base,2026)['securities']['9900']
        for key,value in manual.items():self.assertEqual(fact[key],value)
        self.assertEqual(fact['month_open_close'],120);self.assertEqual(fact['month_open_close_date'],'2026-11-02')
        self.assertEqual(fact['month_open_calendar_source'],CALENDAR_SOURCE)

    def test_confirmed_opening_and_previous_manual_values_skip_provider_and_preserve_bytes(self):
        self.seed({'9900':opening_prices()});before=self.path.read_bytes();provider=Mock()
        result=self.refresh(provider);provider.assert_not_called();self.assertFalse(result['written'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_weekday_legacy_literal_price_can_be_reused_only_for_same_actual_date(self):
        old=dict(close=100,close_date='2026-09-30',price_source='Manual previous',
                 month_start_close=120,month_start_close_date='2026-10-01',month_start_price_source='Manual first')
        self.seed({'9900':old});before=self.path.read_bytes();provider=Mock()
        result=self.refresh(provider,'2026-10',datetime(2026,10,7,tzinfo=JST))
        provider.assert_not_called();self.assertFalse(result['written']);self.assertEqual(self.path.read_bytes(),before)

    def test_closed_literal_one_cache_does_not_substitute_for_expected_november_two(self):
        old=dict(close=100,close_date='2026-10-30',month_start_close=777,month_start_close_date='2026-11-01')
        self.seed({'9900':old});provider=Mock(return_value=opening_prices())
        self.refresh(provider);provider.assert_called_once()
        fact=load_research(self.base,2026)['securities']['9900']
        self.assertEqual(fact['month_start_close'],777);self.assertEqual(fact['month_open_close'],120)

    def test_stale_new_field_does_not_hide_valid_same_date_legacy_business_day_price(self):
        old=dict(close=100,close_date='2026-09-30',price_source='Manual previous',
                 month_open_close=7,month_open_close_date='2026-09-01',month_open_price_source='Old month',
                 month_start_close=120,month_start_close_date='2026-10-01',month_start_price_source='Manual first')
        self.seed({'9900':old});before=self.path.read_bytes();provider=Mock()
        self.refresh(provider,'2026-10',datetime(2026,10,7,tzinfo=JST))
        provider.assert_not_called();self.assertEqual(self.path.read_bytes(),before)

    def test_later_date_unproven_source_bad_values_and_conflicting_first_day_fail_without_overwrite(self):
        old=dict(close=100,close_date='2026-10-30',month_open_close=7,month_open_close_date='2026-10-01',month_open_price_source='Old month')
        for invalid in (opening_prices(month_open_close_date='2026-11-04'),
                        opening_prices(month_open_price_source=''),opening_prices(month_open_close=0),
                        opening_prices(month_open_close=-1),opening_prices(month_open_close=True),
                        opening_prices(month_open_close=float('nan')),
                        dict(close=100,close_date='2026-10-30',month_start_close=777,month_start_close_date='2026-11-01')):
            self.seed({'9900':old});before=self.path.read_bytes()
            with self.subTest(invalid=invalid):
                result=self.refresh(Mock(return_value=invalid))
                self.assertFalse(result['written']);self.assertEqual(result['failed'],['9900'])
                self.assertEqual(self.path.read_bytes(),before)

    def test_partial_previous_observation_is_kept_and_missing_open_is_reported(self):
        result=self.refresh(Mock(return_value=dict(close=100,close_date='2026-10-30',price_source='Synthetic')))
        self.assertEqual(result['updated'],1);self.assertEqual(result['failed'],['9900'])
        self.assertNotIn('month_open_close',load_research(self.base,2026)['securities']['9900'])

    def test_new_first_day_fields_are_numeric_iso_and_source_typed_when_loading(self):
        for bad in ({'month_open_close':True},{'month_open_close':float('nan')},
                    {'month_open_close_date':'2026/11/2'},{'month_open_price_source':{}},
                    {'month_open_calendar_source':[]}):
            self.seed({'9900':bad});self.assertTrue(load_research(self.base,2026).get('_load_error'))

    def test_public_refresh_uses_new_open_policy_and_subsequently_skips_confirmed_prices(self):
        provider=Mock(return_value=opening_prices());dividend=Mock(return_value=reference())
        result=refresh_public_research(self.base,['9900'],'2026-11',now=NOV,price_provider=provider,dividend_provider=dividend)
        self.assertEqual(result['price_failed'],[]);self.assertEqual(provider.call_count,1)
        self.assertEqual(load_research(self.base,2026)['securities']['9900']['month_open_close'],120)
        provider.reset_mock()
        result=refresh_public_research(self.base,['9900'],'2026-11',now=NOV,price_provider=provider,dividend_provider=dividend)
        provider.assert_not_called();self.assertEqual(result['price_failed'],[])


if __name__=='__main__':unittest.main()
