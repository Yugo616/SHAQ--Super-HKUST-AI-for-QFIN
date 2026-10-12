import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx

from shaq_daily_oracle.alpaca_bars import AlpacaBarsError
from shaq_daily_oracle import alpaca_trades

ET = ZoneInfo('America/New_York')
NOW = datetime(2026, 10, 8, 8, 49, tzinfo=ET)


def trade(i=1, **changes):
    return dict(t='2026-10-08T12:01:00Z', p=176.5, s=1, i=i, x='D', c=['I'], z='A', **changes)


class TradeTests(unittest.TestCase):
    def collect(self, handler, **changes):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return alpaca_trades.collect_premarket_trades(**dict(
                symbols=['DVA'], session_date=date(2026, 10, 8), cutoff=NOW,
                observed_at=NOW, key_id='private', secret_key='secret',
                delay_seconds=900, safety_margin_seconds=60, timeout_seconds=1,
                clock=lambda: NOW, client=client, retry_backoff_seconds=0,
                **changes))

    def test_odd_lot_prints_survive_without_inventing_bars_or_aggressor(self):
        result = self.collect(lambda r: httpx.Response(200, json={
            'trades': {'DVA': [trade(), trade(2)]}, 'next_page_token': None}))
        row = result['symbols']['DVA']
        self.assertEqual(row['reported_trade_volume'], 2)
        self.assertEqual(row['odd_lot_volume'], 2)
        self.assertEqual(row['trades'][0]['conditions'], ['I'])
        self.assertIsNone(row['native_aggressor'])
        self.assertNotIn('bars', row)
        self.assertNotIn('private', str(result))

    def test_pagination_deduplicates_by_venue_trade_id_not_time(self):
        def response(r):
            self.assertEqual(r.url.host, 'data.alpaca.markets')
            self.assertEqual(r.url.params['end'], '2026-10-08T08:33:00-04:00')
            return httpx.Response(200, json={'trades': {'DVA': [trade(), trade(2)]},
                'next_page_token': None if 'page_token' in r.url.params else 'page2'})
        result = self.collect(response)
        self.assertEqual(result['symbols']['DVA']['trade_count'], 2)
        self.assertEqual(len(result['raw_pages']), 2)

    def test_rejects_wrong_day_or_future_or_conflicting_print(self):
        for rows in ([{**trade(), 't': '2026-10-07T12:00:00Z'}],
                     [{**trade(), 't': '2026-10-08T12:34:00Z'}],
                     [trade(), {**trade(), 'p': 200}]):
            with self.subTest(rows=rows), self.assertRaises(AlpacaBarsError):
                self.collect(lambda r: httpx.Response(200, json={'trades': {'DVA': rows}, 'next_page_token': None}))

    def test_auth_not_retried_and_transient_failure_is_retried(self):
        for status, count_expected in ((403, 1), (503, 2)):
            calls = []
            def response(r):
                calls.append(r)
                return httpx.Response(status)
            with self.assertRaises(AlpacaBarsError):
                self.collect(response)
            self.assertEqual(len(calls), count_expected)

    def test_empty_is_not_zero_activity_and_late_capture_rejected(self):
        result = self.collect(lambda r: httpx.Response(200, json={'trades': {}, 'next_page_token': None}))
        self.assertIsNone(result['symbols']['DVA']['reported_trade_volume'])
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,
                json={'trades': {'DVA': [trade()]}, 'next_page_token': None}))) as client:
            with self.assertRaises(AlpacaBarsError):
                alpaca_trades.collect_premarket_trades(symbols=['DVA'],session_date=NOW.date(),
                    cutoff=NOW, observed_at=NOW,key_id='private',secret_key='secret',delay_seconds=900,
                    safety_margin_seconds=60,timeout_seconds=1,client=client,
                    clock=lambda: datetime(2026,10,8,9,0,tzinfo=ET))
