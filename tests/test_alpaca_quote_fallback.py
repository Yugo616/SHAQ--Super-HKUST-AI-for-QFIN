import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from shaq_daily_oracle import alpaca_iex_quotes as quotes


def quote(t, size=3, **fields):
    return dict(t=t, bp=100.0, ap=100.1, bs=size, **{'as': 4},
                bx='Q', ax='P', c=['R'], z='C', **fields)


class QuoteFallbackTests(unittest.TestCase):
    def test_conflicting_equal_time_quotes_are_excluded_before_pressure_accumulation(self):
        from itertools import permutations
        from shaq_daily_oracle.iex_orderflow import compute_sip_quote_pressure
        first = dict(quote('2026-10-05T12:20:00Z', 3), T='q', S='TEST')
        conflict = [dict(quote('2026-10-05T12:20:01Z', size), T='q', S='TEST')
                    for size in (100, 1)]
        after = dict(quote('2026-10-05T12:20:02Z', 5), T='q', S='TEST')
        results = []
        for group in permutations(conflict):
            result = compute_sip_quote_pressure([first, *group, after],
                symbol='TEST', cutoff='2026-10-05T12:22:00Z')
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['event_count'], 0)
            self.assertEqual(result['excluded_quote_count'], 2)
            self.assertIsNone(result['ofi_quote_size'])
            self.assertAlmostEqual(result['average_visible_depth_quote_size_per_side'], 4)
            results.append(result)
        self.assertEqual(results[0], results[1])

    def test_identical_sip_duplicates_contribute_once(self):
        from shaq_daily_oracle.iex_orderflow import compute_sip_quote_pressure
        first = dict(quote('2026-10-05T12:20:00Z', 3), T='q', S='TEST')
        last = dict(quote('2026-10-05T12:20:01Z', 5), T='q', S='TEST')
        result = compute_sip_quote_pressure([first, first.copy(), last, last.copy()],
            symbol='TEST', cutoff='2026-10-05T12:22:00Z')
        self.assertEqual(result['event_count'], 1)
        self.assertEqual(result['ofi_quote_size'], 2)

    def run_collector(self, fetch, **kw):
        return quotes.collect_quote_pressure(
            key_id='private-key', secret='private-secret', symbols=['TEST'],
            cutoff='2026-10-05T08:50:00-04:00', observed_at='2026-10-05T08:38:00-04:00',
            window_seconds=120, max_pages_per_symbol=2, timeout_seconds=1,
            max_response_bytes=10000, max_attempts=1, delay_seconds=900,
            safety_margin_seconds=60, fetch=fetch,
            clock=lambda: datetime(2026, 10, 5, 12, 39, tzinfo=timezone.utc), **kw)

    def test_empty_iex_uses_delayed_sip_and_keeps_actual_window_and_source(self):
        self.assertTrue(hasattr(quotes, 'collect_quote_pressure'), 'Missing delayed SIP fallback')
        calls=[]
        def fetch(req, timeout, limit):
            query=parse_qs(urlparse(req.full_url).query); calls.append(query)
            rows=[] if query['feed']==['iex'] else [
                quote('2026-10-05T12:20:01Z'), quote('2026-10-05T12:21:00Z', 5)]
            return json.dumps({'quotes': {'TEST': rows}, 'next_page_token': None}).encode()
        result=self.run_collector(fetch); row=result['symbols']['TEST']
        self.assertEqual([x['feed'][0] for x in calls], ['iex', 'sip'])
        self.assertEqual(calls[1]['start'], ['2026-10-05T12:20:00Z'])
        self.assertEqual(calls[1]['end'], ['2026-10-05T12:22:00Z'])
        self.assertEqual(row['feed'], 'sip')
        self.assertEqual(row['calculation']['ofi_quote_size'], 2)
        self.assertEqual(row['calculation']['unit'], 'shares')
        self.assertFalse(row['calculation']['native_trade_aggressor'])
        self.assertFalse(row['calculation']['full_market_capital_flow'])
        self.assertTrue(row['formal_cutoff_eligible'])
        self.assertEqual(row['requested_end'], '2026-10-05T12:22:00Z')
        self.assertEqual(row['attempts'][0]['quote_count'], 0)
        self.assertNotIn('private-secret', json.dumps(result))

    def test_sip_pages_are_complete_before_evidence_can_be_used(self):
        self.assertTrue(hasattr(quotes, 'collect_quote_pressure'), 'Missing delayed SIP fallback')
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query)
            return json.dumps({'quotes': {'TEST': []},
                'next_page_token': 'more' if q['feed']==['sip'] else None}).encode()
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertEqual(row['status'], 'provider_error')
        self.assertFalse(row['formal_cutoff_eligible'])

    def test_sip_is_not_relabelled_iex_and_cannot_enter_after_cutoff(self):
        self.assertTrue(hasattr(quotes, 'collect_quote_pressure'), 'Missing delayed SIP fallback')
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query)
            rows=[] if q['feed']==['iex'] else [quote('2026-10-05T12:20:01Z'), quote('2026-10-05T12:21:00Z',5)]
            return json.dumps({'quotes': {'TEST': rows}, 'next_page_token': None}).encode()
        result=quotes.collect_iex_quotes(key_id='k',secret='s',symbols=['TEST'],
            cutoff='2026-10-05T08:50:00-04:00', observed_at='2026-10-05T08:38:00-04:00',
            window_seconds=120,max_pages_per_symbol=2,timeout_seconds=1,
            max_response_bytes=10000,max_attempts=1,feed='sip',delay_seconds=900,safety_margin_seconds=60,
            fetch=fetch,clock=lambda:datetime(2026,10,5,12,51,tzinfo=timezone.utc))
        self.assertFalse(result['symbols']['TEST']['formal_cutoff_eligible'])
        self.assertIn('SIP', result['scope'])

    def test_quote_calculation_does_not_claim_trade_direction(self):
        from shaq_daily_oracle import iex_orderflow
        self.assertTrue(hasattr(iex_orderflow,'compute_sip_quote_pressure'), 'Missing honest SIP calculation')
        calc=iex_orderflow.compute_sip_quote_pressure([
            dict(quote('2026-10-05T12:20:01Z'),T='q',S='TEST'),
            dict(quote('2026-10-05T12:21:00Z',5),T='q',S='TEST')],
            symbol='TEST', cutoff='2026-10-05T12:22:00Z')
        self.assertEqual(calc['ofi_quote_size'], 2)
        self.assertEqual(calc['event_count'], 1)
        self.assertFalse(calc['native_trade_aggressor'])
        self.assertIn('quote',calc['inference_scope'])

    def test_authentication_error_does_not_retry_another_feed(self):
        from urllib.error import HTTPError
        calls=[]
        def fetch(req, timeout, limit):
            calls.append(req.full_url)
            raise HTTPError(req.full_url,401,'Unauthorized',{},None)
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertEqual(len(calls),1)
        self.assertEqual(row['http_status'],401)
        self.assertFalse(row['formal_cutoff_eligible'])

    def test_exchange_switch_invalid_book_and_future_quote_cannot_create_pressure(self):
        from shaq_daily_oracle.iex_orderflow import compute_sip_quote_pressure
        rows=[dict(quote('2026-10-05T12:20:01Z'),T='q',S='TEST'),
              dict(quote('2026-10-05T12:20:02Z',99),T='q',S='TEST',bx='N'),
              dict(quote('2026-10-05T12:20:03Z'),T='q',S='TEST',ap=99),
              dict(quote('2026-10-05T12:20:04Z'),T='q',S='TEST'),
              dict(quote('2026-10-05T12:23:00Z',99999),T='q',S='TEST')]
        calc=compute_sip_quote_pressure(rows,symbol='TEST',cutoff='2026-10-05T12:22:00Z')
        self.assertIsNone(calc['ofi_quote_size'])
        self.assertEqual(calc['event_count'],0)
        self.assertEqual(calc['venue_switch_count'],1)
        self.assertEqual(calc['excluded_quote_count'],1)

    def test_sparse_quotes_are_recovered_from_session_without_claiming_recent_pressure(self):
        queries=[]
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query); queries.append(q)
            rows=([quote('2026-10-05T12:08:00Z'),quote('2026-10-05T12:09:00Z',5)]
                  if q['start']==['2026-10-05T08:00:00Z'] else [])
            return json.dumps({'quotes':{'TEST':rows},'next_page_token':None}).encode()
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertEqual(row['status'],'liquidity_context')
        self.assertTrue(row['formal_cutoff_eligible'])
        self.assertEqual(row['quote_count'],2)
        self.assertEqual(row['window_seconds'],15720)
        self.assertEqual(row['recent_window_seconds'],120)
        self.assertEqual(row['calculation']['last_quote_time'],'2026-10-05T12:09:00Z')
        self.assertEqual(row['calculation']['last_quote_age_seconds'],780)
        self.assertIsNone(row['calculation']['ofi_quote_size'])
        self.assertFalse(row['calculation']['recent_pressure_available'])
        self.assertEqual(row['calculation']['session_context']['ofi_quote_size'],2)
        self.assertEqual(len(row['attempts']),3)
        self.assertEqual(queries[-1]['end'],['2026-10-05T12:22:00Z'])

    def test_single_recent_quote_is_useful_liquidity_not_invented_order_flow(self):
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query)
            rows=[quote('2026-10-05T12:21:00Z')] if q['feed']==['sip'] else []
            return json.dumps({'quotes':{'TEST':rows},'next_page_token':None}).encode()
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertEqual(row['status'],'liquidity_context')
        self.assertEqual(row['calculation']['latest_quote']['bid_price'],100)
        self.assertIsNone(row['calculation']['ofi_quote_size'])
        self.assertTrue(row['formal_cutoff_eligible'])

    def test_rate_limit_waits_before_retry_and_preserves_request(self):
        from urllib.error import HTTPError
        calls=[]; waits=[]
        def fetch(req, timeout, limit):
            calls.append(req.full_url)
            if len(calls)==1:raise HTTPError(req.full_url,429,'rate limit',{'Retry-After':'2'},None)
            return b'{"quotes":{},"next_page_token":null}'
        from urllib.request import Request
        result=quotes._fetch_bounded(Request('https://data.alpaca.markets/v2/stocks/quotes'),
            timeout=1,max_bytes=10000,max_attempts=2,fetch=fetch,sleep=waits.append)
        self.assertEqual(waits,[2])
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(json.loads(result)['quotes'],{})

    def test_expansion_error_preserves_already_retrieved_quote(self):
        from urllib.error import HTTPError
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query)
            if q['start']==['2026-10-05T08:00:00Z']:
                raise HTTPError(req.full_url,503,'temporary',{},None)
            rows=[quote('2026-10-05T12:21:00Z')] if q['feed']==['sip'] else []
            return json.dumps({'quotes':{'TEST':rows},'next_page_token':None}).encode()
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertEqual(row['status'],'liquidity_context')
        self.assertEqual(row['quote_count'],1)
        self.assertTrue(row['formal_cutoff_eligible'])
        self.assertEqual(row['attempts'][-1]['http_status'],503)

    def test_session_query_uses_new_york_winter_time_and_rejects_other_day_quotes(self):
        queries=[]
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query); queries.append(q)
            return json.dumps({'quotes':{'TEST':[
                quote('2026-01-05T13:00:00Z'),quote('2026-01-06T13:00:00Z'),
                quote('2026-01-06T13:01:00Z',5),quote('2026-01-06T15:00:00Z',99)]},
                'next_page_token':None}).encode()
        result=quotes.collect_iex_quotes(key_id='k',secret='s',symbols=['TEST'],
            cutoff='2026-01-06T08:50:00-05:00',observed_at='2026-01-06T08:38:00-05:00',
            window_seconds=120,max_pages_per_symbol=2,timeout_seconds=1,max_response_bytes=10000,
            max_attempts=1,feed='sip',delay_seconds=900,safety_margin_seconds=60,
            full_premarket_session=True,fetch=fetch,
            clock=lambda:datetime(2026,1,6,13,40,tzinfo=timezone.utc))
        row=result['symbols']['TEST']
        self.assertEqual(queries[0]['start'],['2026-01-06T09:00:00Z'])
        self.assertEqual(row['quote_count'],2)
        self.assertIsNone(row['calculation']['ofi_quote_size'])
        self.assertEqual(row['calculation']['session_context']['ofi_quote_size'],2)

    def test_long_retry_after_is_not_ignored_or_retried_immediately(self):
        from urllib.error import HTTPError
        from urllib.request import Request
        calls=[];waits=[]
        def fetch(req,timeout,limit):
            calls.append(req.full_url)
            raise HTTPError(req.full_url,429,'rate limit',{'Retry-After':'300'},None)
        with self.assertRaises(HTTPError):
            quotes._fetch_bounded(Request('https://data.alpaca.markets/v2/stocks/quotes'),
                timeout=1,max_bytes=10000,max_attempts=2,fetch=fetch,sleep=waits.append)
        self.assertEqual(len(calls),1)
        self.assertEqual(waits,[])

    def test_conflicting_single_timestamp_is_not_published_as_valid_liquidity(self):
        def fetch(req, timeout, limit):
            q=parse_qs(urlparse(req.full_url).query)
            rows=([quote('2026-10-05T12:21:00Z'),quote('2026-10-05T12:21:00Z',99)]
                  if q['feed']==['sip'] else [])
            return json.dumps({'quotes':{'TEST':rows},'next_page_token':None}).encode()
        row=self.run_collector(fetch)['symbols']['TEST']
        self.assertFalse(row['formal_cutoff_eligible'])
        self.assertIsNone(row['calculation']['latest_quote'])

if __name__=='__main__': unittest.main()
