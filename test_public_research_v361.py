"""Dated public-page fixtures; no live website or personal account is used."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from market_data import load_research, refresh_public_research
from public_research import (MAX_PUBLIC_BYTES, PublicReferenceError, _search_url,
                             fetch_investing_dividends, investing_url,
                             merge_investing_reference, parse_investing_dividends,
                             parse_yahoo_history, public_text,
                             yahoo_japan_month_closes)

JST=timezone(timedelta(hours=9))
NOW=datetime(2026,11,7,12,tzinfo=JST)
SOURCE='https://jp.investing.com/equities/example-dividends'


def yahoo_html(rows,code='9900'):
    cells=''.join('<tr>'+''.join('<td>'+str(value)+'</td>' for value in row)+'</tr>' for row in rows)
    return ('<html><h1>Example【'+code+'】時系列</h1><table><thead><tr>'
            '<th>日付</th><th>始値</th><th>高値</th><th>安値</th><th>終値</th>'
            '<th>出来高</th><th>調整後終値</th></tr></thead><tbody>'+cells+'</tbody></table></html>')


def investing_html(rows=(),code='9900',extra=''):
    cells=''.join('<tr>'+''.join('<td>'+str(value)+'</td>' for value in row)+'</tr>' for row in rows)
    return ('<html><h1>Example Corp ('+code+')</h1><table><tr><th>権利落ち日</th>'
            '<th>配当</th><th>タイプ</th><th>支払日</th><th>利回り</th></tr>'+cells+'</table>'+extra+'</html>')


class Response(io.BytesIO):
    def __init__(self,text,url):
        super().__init__(text.encode() if isinstance(text,str) else text);self.url=url
    def geturl(self):return self.url


def reference(**extra):
    value=dict(code='9900',year=2026,source=SOURCE,
               events=[dict(ex_date='2026-09-29',payment='2026-11-26',payment_year=2026,dps=5)])
    value.update(extra);return value


def prices(**extra):
    value=dict(close=120,close_date='2026-10-30',price_source='Synthetic Yahoo historical',
               month_start_close=125,month_start_close_date='2026-11-01',
               month_start_price_source='Synthetic literal first-day fixture')
    value.update(extra);return value


class YahooJapanHistory(unittest.TestCase):
    def test_visible_unadjusted_close_is_selected_instead_of_adjusted_or_current_quote(self):
        text=yahoo_html([('2026年10月30日',1,2,0,120,999,777),
                         ('2026年11月1日',1,2,0,125,999,888)])
        self.assertEqual(parse_yahoo_history(text,'9900'),[(date(2026,10,30),120),(date(2026,11,1),125)])
        opener=Mock(side_effect=lambda req,**kw:Response(text,req.full_url))
        result=yahoo_japan_month_closes('9900','2026-11',opener=opener,now=NOW)
        self.assertEqual(result['close'],120);self.assertEqual(result['month_start_close'],125)
        self.assertIn('/9900.T/history?',result['price_source'])
        self.assertIn('from=20261001',opener.call_args.args[0].full_url)
        self.assertIn('to=20261102',opener.call_args.args[0].full_url)

    def test_holiday_has_no_literal_first_day_price_and_next_day_is_not_substituted(self):
        text=yahoo_html([('2026/10/30',1,2,0,120,999,777),('2026/11/2',1,2,0,999,999,888)])
        result=yahoo_japan_month_closes('9900','2026-11',opener=lambda req,**kw:Response(text,req.full_url),now=NOW)
        self.assertEqual(result['close'],120);self.assertNotIn('month_start_close',result)

    def test_wrong_symbol_challenge_and_conflicting_duplicate_prices_are_not_accepted(self):
        with self.assertRaises(PublicReferenceError):parse_yahoo_history(yahoo_html([],code='9901'),'9900')
        with self.assertRaises(PublicReferenceError):parse_yahoo_history('<title>Verify you are human</title>','9900')
        text=yahoo_html([('2026/10/30',1,2,0,120,999,777),('2026/10/30',1,2,0,121,999,777)])
        self.assertEqual(parse_yahoo_history(text,'9900'),[])

    def test_only_symbol_bound_historical_json_is_used(self):
        payload={'symbol':'9900.T','historicalData':[{'date':'2026-10-30','close':120}],
                 'recommendations':[{'symbol':'9901.T','historicalData':[{'date':'2026-10-30','close':999}]}]}
        text='<h1>Example (9900)</h1><script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload)+'</script>'
        self.assertEqual(parse_yahoo_history(text,'9900'),[(date(2026,10,30),120)])
        payload={'unrelated':{'date':'2026-10-30','close':999},
                 'other':{'symbol':'9901.T','historicalData':[{'date':'2026-10-30','close':999}]}}
        text='<h1>Example (9900)</h1><script type="application/json">'+json.dumps(payload)+'</script>'
        self.assertEqual(parse_yahoo_history(text,'9900'),[])

    def test_embedded_foreign_symbol_cannot_inherit_a_parent_history_container(self):
        payload={'symbol':'9900.T','historicalData':[{'date':'2026-10-30','close':120},
                    {'symbol':'9901.T','date':'2026-10-30','close':999}]}
        text='<h1>Example (9900)</h1><script type="application/json">'+json.dumps(payload)+'</script>'
        self.assertEqual(parse_yahoo_history(text,'9900'),[(date(2026,10,30),120)])
        text='<h1>Wrong Company (9901)</h1><script type="application/json">'+json.dumps(payload)+'</script>'
        with self.assertRaises(PublicReferenceError):parse_yahoo_history(text,'9900')

    def test_incomplete_today_bar_and_future_month_are_rejected_before_fetch(self):
        text=yahoo_html([('2026/9/30',1,2,0,120,999,777),('2026/10/1',1,2,0,125,999,888)])
        now=datetime(2026,10,1,15,59,tzinfo=JST)
        result=yahoo_japan_month_closes('9900','2026-10',opener=lambda req,**kw:Response(text,req.full_url),now=now)
        self.assertNotIn('month_start_close',result)
        opener=Mock()
        with self.assertRaises(PublicReferenceError):yahoo_japan_month_closes('9900','2027-01',opener=opener,now=NOW)
        opener.assert_not_called()

    def test_default_month_closes_uses_japanese_page_without_importing_yfinance(self):
        from yahoo_market import month_closes
        supplied=prices()
        with patch('public_research.yahoo_japan_month_closes',return_value=supplied),patch('yfinance.Ticker',side_effect=AssertionError('no fallback')):
            self.assertEqual(month_closes('9900','2026-11',now=NOW),supplied)

    def test_blocked_japanese_page_falls_back_to_existing_unadjusted_yahoo_client(self):
        from yahoo_market import month_closes
        ticker=Mock();ticker.history.return_value=pd.DataFrame({'Close':[120,125]},index=pd.to_datetime(['2026-10-30','2026-11-01']))
        with patch('public_research.yahoo_japan_month_closes',side_effect=PublicReferenceError('blocked')),patch('yfinance.Ticker',return_value=ticker):
            result=month_closes('9900','2026-11',now=NOW)
        self.assertEqual(result['close'],120);self.assertEqual(result['month_start_close'],125)
        self.assertFalse(ticker.history.call_args.kwargs['auto_adjust'])


class InvestingDividendPages(unittest.TestCase):
    def test_payment_date_is_not_the_ex_date_and_rows_filter_current_calendar_year(self):
        text=investing_html([('2026年9月29日','5','中間','2026年11月26日','1%'),
                             ('2025年12月29日','4','期末','2026年3月5日','1%'),
                             ('2025年9月29日','3','中間','2025年11月26日','1%')])
        result=parse_investing_dividends(text,'9900',2026,SOURCE)
        self.assertEqual(len(result['events']),2)
        event=result['events'][1]
        self.assertEqual(event['ex_date'],'2026-09-29');self.assertEqual(event['payment'],'2026-11-26')
        self.assertEqual(event['dps'],5);self.assertNotIn('rights',event)

    def test_empty_or_unknown_payment_does_not_mean_no_dividend(self):
        for text in (investing_html(),investing_html([('2026/9/29','5','中間','-','0%')])):
            result=parse_investing_dividends(text,'9900',2026,SOURCE)
            self.assertNotIn('no_dividend',result)
        unknown=parse_investing_dividends(investing_html([('2026/9/29','5','中間','未定','0%')]),'9900',2026,SOURCE)
        self.assertIsNone(unknown['events'][0]['payment'])

    def test_zero_policy_must_be_a_current_instrument_statement_not_footer_or_script(self):
        positive=parse_investing_dividends(investing_html(extra='<p>Example Corp does not currently pay dividends.</p>'),'9900',2026,SOURCE)
        self.assertTrue(positive['no_dividend']);self.assertEqual(positive['no_dividend_source'],SOURCE)
        for extra in ('<footer><p>Unrelated Corp does not pay dividends.</p></footer>',
                      '<p>無配方針の銘柄もあります。</p>',
                      '<script>const x="Example Corp does not pay dividends";</script>',
                      '<p>Example Corp (9900) pays dividends; Other Corp (9901) does not pay dividends.</p>',
                      '<p>Compared with Example Corp (9900), Other Corp does not pay dividends.</p>',
                      '<p>Example Corp pays dividends. Example Corp does not pay dividends.</p>',
                      '<p>Example Corp (9900)は無配ですか？</p>',
                      '<p>Example Corp (9900) does not pay dividends?</p>',
                      '<p>If Example Corp does not pay dividends, income will fall.</p>',
                      '<p>Example Corp (9900)が無配である場合、注意してください。</p>',
                      '<p>It is false that Example Corp does not pay dividends.</p>'):
            result=parse_investing_dividends(investing_html(extra=extra),'9900',2026,SOURCE)
            self.assertNotIn('no_dividend',result)

    def test_known_positive_current_dividend_overrides_a_conflicting_zero_policy_sentence(self):
        text=investing_html([('2026/9/29',5,'中間','2026/11/26','1%')],extra='<p>9900 does not pay dividends.</p>')
        self.assertNotIn('no_dividend',parse_investing_dividends(text,'9900',2026,SOURCE))

    def test_structured_dividends_require_owned_symbol_container(self):
        own={'symbol':'9900','dividends':[{'exDate':'2026-09-29','paymentDate':'2026-11-26','dividend':5}],
             'relatedInstruments':[{'symbol':'9901','dividends':[{'exDate':'2026-09-29','paymentDate':'2026-12-31','dividend':999}]}]}
        text=investing_html(extra='<script type="application/json">'+json.dumps(own)+'</script>')
        self.assertEqual([x['payment'] for x in parse_investing_dividends(text,'9900',2026,SOURCE)['events']],['2026-11-26'])
        unrelated={'date':'2026-09-29','paymentDate':'2026-11-26','dividend':999}
        text=investing_html(extra='<script type="application/json">'+json.dumps(unrelated)+'</script>')
        self.assertEqual(parse_investing_dividends(text,'9900',2026,SOURCE)['events'],[])

    def test_instrument_mismatch_duplicate_payment_conflict_and_unsafe_sources_fail(self):
        with self.assertRaises(PublicReferenceError):parse_investing_dividends(investing_html(code='9901'),'9900',2026,SOURCE)
        with self.assertRaises(PublicReferenceError):parse_investing_dividends(investing_html(),'9900',2026,'https://evil.invalid/equities/example-dividends')
        text=investing_html([('2026/9/29',5,'中間','2026/11/26','1%'),('2026/9/29',5,'中間','2026/11/27','1%')])
        with self.assertRaises(PublicReferenceError):parse_investing_dividends(text,'9900',2026,SOURCE)

    def test_existing_source_slug_is_used_before_search_and_ticker_still_verified(self):
        html=investing_html([('2026/9/29',5,'中間','2026/11/26','1%')])
        opener=Mock(side_effect=lambda req,**kw:Response(html,req.full_url))
        result=fetch_investing_dividends('9900',2026,{'source':'Official ; https://jp.investing.com/equities/example-historical-data ; verified'},opener=opener)
        self.assertEqual(result['source'],SOURCE);self.assertEqual(opener.call_count,1)
        self.assertEqual(opener.call_args.args[0].full_url,SOURCE)

    def test_unknown_symbol_search_requires_exact_code_and_japanese_exchange(self):
        quotes={'quotes':[{'symbol':'9900','exchange':'Tokyo','link':'/equities/example'},
                          {'symbol':'9900','exchange':'London','link':'/equities/foreign'}]}
        self.assertEqual(_search_url(json.dumps(quotes),'9900'),SOURCE)
        def opener(req,**options):
            if req.full_url.startswith('https://api.investing.com/'):
                return Response(json.dumps(quotes),req.full_url)
            return Response(investing_html([('2026/9/29',5,'中間','2026/11/26','1%')]),req.full_url)
        result=fetch_investing_dividends('9900',2026,opener=opener)
        self.assertEqual(result['events'][0]['payment'],'2026-11-26')
        quotes['quotes'].append({'symbol':'9900','exchange':'Tokyo','link':'/equities/other'})
        self.assertIsNone(_search_url(json.dumps(quotes),'9900'))

    def test_only_safe_source_slugs_are_accepted(self):
        self.assertEqual(investing_url('https://jp.investing.com/equities/example-historical-data'),SOURCE)
        for value in ('https://evil.invalid/equities/example','https://jp.investing.com@evil.invalid/equities/example',
                      'https://jp.investing.com/etfs/example','https://jp.investing.com/equities/example?url=secret'):
            self.assertIsNone(investing_url(value))


class ResearchMerge(unittest.TestCase):
    def test_verified_entitlement_gets_missing_payment_without_replacing_annual_or_manual_facts(self):
        original={'name':'Manual name','source':'Official manual source','dps':12,'calendar_complete':True,
                  'payments':'2026/06/04・中間支払日は未確認',
                  'events':[dict(rights='2026-09-28',dps=5,payment_year=2026)]}
        before=deepcopy(original);incoming=reference();incoming_before=deepcopy(incoming)
        result=merge_investing_reference(original,incoming,2026,'9900')
        self.assertEqual(original,before);self.assertEqual(incoming,incoming_before)
        self.assertEqual(result['dps'],12);self.assertEqual(result['source'],'Official manual source')
        self.assertEqual(result['events'][0]['rights'],'2026-09-28')
        self.assertEqual(result['events'][0]['payment'],'2026-11-26')
        self.assertIn('2026-11-26',result['payments'])

    def test_unknown_rights_never_get_a_weekday_approximation_or_complete_calendar(self):
        result=merge_investing_reference({},reference(),2026,'9900')
        self.assertNotIn('rights',result['events'][0]);self.assertNotIn('calendar_complete',result)

    def test_conflicting_verified_payment_is_preserved_and_not_added_to_display_dates(self):
        original={'payments':'2026-11-25','events':[dict(ex_date='2026-09-29',rights='2026-09-28',payment='2026-11-25',dps=5)]}
        result=merge_investing_reference(original,reference(),2026,'9900')
        self.assertEqual(result['payments'],'2026-11-25');self.assertEqual(result['events'],original['events'])
        self.assertEqual(result['investing_reference']['events'][0]['payment'],'2026-11-26')

    def test_wrong_code_year_negative_nonfinite_or_boolean_reference_is_rejected(self):
        for invalid in (reference(code='9901'),reference(year=2025),reference(no_dividend='yes'),
                        reference(events=[dict(ex_date='2026-09-29',payment='2026-11-26',dps=True)]),
                        reference(events=[dict(ex_date='2026-09-29',payment='2026-11-26',dps=float('nan'))])):
            with self.subTest(invalid=invalid),self.assertRaises(PublicReferenceError):merge_investing_reference({},invalid,2026,'9900')


class CombinedResearchRefresh(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.folder=self.base/'market';self.folder.mkdir()
        self.path=self.folder/'research_2026.json';self.seed({})

    def seed(self,facts):self.path.write_text(json.dumps(dict(year=2026,securities=facts)),encoding='utf-8')
    def refresh(self,**options):
        return refresh_public_research(self.base,['9900'],'2026-11',now=NOW,
                                       price_provider=options.pop('price_provider',Mock(return_value=prices())),
                                       dividend_provider=options.pop('dividend_provider',Mock(return_value=reference())),**options)

    def test_prices_and_payment_dates_persist_together_without_overwriting_either(self):
        self.seed({'9900':dict(dps=12,source='Manual',calendar_complete=True,events=[dict(rights='2026-09-28',dps=5)])})
        result=self.refresh();self.assertTrue(result['written']);self.assertEqual(result['failed'],[])
        fact=load_research(self.base,2026)['securities']['9900']
        self.assertEqual(fact['close'],120);self.assertEqual(fact['month_start_close'],125)
        self.assertEqual(fact['events'][0]['payment'],'2026-11-26');self.assertEqual(fact['dps'],12)

    def test_current_year_dividends_are_independent_of_selected_prior_year_price_period(self):
        result=refresh_public_research(self.base,['9900'],'2025-11',now=NOW,
                  price_provider=Mock(return_value=prices(close_date='2025-10-31',month_start_close_date='2025-11-01')),
                  dividend_provider=Mock(return_value=reference()))
        self.assertTrue(result['written']);self.assertTrue((self.folder/'research_2025.json').is_file())
        self.assertIn('2026-11-26',load_research(self.base,2026)['securities']['9900']['payments'])

    def test_verified_prices_skip_provider_and_partial_dividend_failure_keeps_manual_facts(self):
        old=dict(prices(),source='Manual',dps=12,events=[]);self.seed({'9900':old})
        price_provider=Mock(side_effect=AssertionError('Known prices must not be fetched'))
        result=self.refresh(price_provider=price_provider,dividend_provider=Mock(side_effect=PublicReferenceError('blocked')))
        price_provider.assert_not_called();self.assertEqual(result['dividend_failed'],['9900'])
        self.assertEqual(load_research(self.base,2026)['securities']['9900'],old)

    def test_verified_zero_policy_does_not_query_investing_or_infer_other_zero_dps_as_zero(self):
        old=dict(prices(),source='Official policy',dps=0,calendar_complete=True,payments='対象外（無分配方針）',events=[])
        self.seed({'9900':old});provider=Mock(side_effect=AssertionError('Known policy'))
        result=self.refresh(dividend_provider=provider);provider.assert_not_called()
        self.assertTrue(load_research(self.base,2026)['securities']['9900']['no_dividend'])
        old.pop('payments');self.seed({'9900':old});provider=Mock(side_effect=PublicReferenceError('unknown'))
        self.refresh(dividend_provider=provider);provider.assert_called_once()
        self.assertNotIn('no_dividend',load_research(self.base,2026)['securities']['9900'])

    def test_cancellation_before_any_read_does_not_write_or_query(self):
        before=self.path.read_bytes();price=Mock();dividend=Mock()
        result=self.refresh(cancelled=lambda:True,price_provider=price,dividend_provider=dividend)
        self.assertTrue(result['cancelled']);price.assert_not_called();dividend.assert_not_called()
        self.assertEqual(self.path.read_bytes(),before)

    def test_timeout_and_late_provider_completion_cannot_write_after_return(self):
        release=threading.Event();self.addCleanup(release.set)
        before=self.path.read_bytes()
        def delayed(*args):release.wait(5);return prices()
        started=time.monotonic()
        result=self.refresh(price_provider=delayed,dividend_provider=delayed,budget_seconds=.03)
        self.assertLess(time.monotonic()-started,.5);self.assertFalse(result['written'])
        self.assertEqual(self.path.read_bytes(),before);release.set();time.sleep(.03)
        self.assertEqual(self.path.read_bytes(),before)

    def test_corrupt_public_cache_is_preserved_instead_of_erasing_user_facts(self):
        self.path.write_bytes(b'{not valid public cache');before=self.path.read_bytes()
        price=Mock();dividend=Mock();result=self.refresh(price_provider=price,dividend_provider=dividend)
        self.assertFalse(result['written']);self.assertEqual(self.path.read_bytes(),before)
        price.assert_not_called();dividend.assert_not_called()

    def test_cache_requires_boolean_no_dividend_and_its_source(self):
        for bad in ({'no_dividend':'yes'},{'no_dividend':True},{'no_dividend':1,'no_dividend_source':'source'}):
            self.seed({'9900':bad});self.assertTrue(load_research(self.base,2026).get('_load_error'))


class PublicHttpBoundaries(unittest.TestCase):
    def test_normal_request_contains_no_authentication_and_is_bounded(self):
        opener=Mock(side_effect=lambda req,**kw:Response('plain public response',req.full_url))
        text,url=public_text('https://finance.yahoo.co.jp/quote/9900.T/history',opener=opener)
        request=opener.call_args.args[0]
        self.assertFalse(any(key.lower() in ('authorization','cookie') for key in request.headers))
        self.assertEqual(opener.call_args.kwargs['timeout'],8);self.assertEqual(text,'plain public response')

    def test_unsafe_hosts_redirects_large_responses_and_blocked_sites_fail(self):
        with self.assertRaises(PublicReferenceError):public_text('https://evil.invalid/',opener=Mock())
        for opener in (lambda req,**kw:Response('x','https://evil.invalid/'),
                       lambda req,**kw:Response(b'x'*(MAX_PUBLIC_BYTES+1),req.full_url),
                       Mock(side_effect=OSError('blocked'))):
            with self.assertRaises(PublicReferenceError):public_text(SOURCE,opener=opener)


if __name__=='__main__':unittest.main()
