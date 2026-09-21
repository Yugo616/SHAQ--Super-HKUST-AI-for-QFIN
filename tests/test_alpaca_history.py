"""Contract tests use documented Alpaca wire responses, not a trading account."""
import json
import unittest
from datetime import date, datetime
from unittest.mock import Mock

import httpx

from shaq_daily_oracle.data_providers import DataProviderError


class AlpacaHistoryTests(unittest.TestCase):
    def provider(self, handler, **kwargs):
        from shaq_daily_oracle.history_fallback import AlpacaHistoryProvider
        return AlpacaHistoryProvider(key_id='fixture-id', secret_key='fixture-secret',
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            now=lambda: datetime.fromisoformat('2026-09-22T08:30:00-04:00'), **kwargs)

    def test_pagination_raw_sip_exclusive_end_and_normalized_times(self):
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(request.url.host, 'data.alpaca.markets')
            self.assertEqual(request.url.params['adjustment'], 'raw')
            self.assertEqual(request.url.params['feed'], 'sip')
            self.assertEqual(request.headers['APCA-API-KEY-ID'], 'fixture-id')
            self.assertEqual(request.url.params['end'], '2026-09-22T00:00:00-04:00')
            symbol = 'AAA' if len(calls) == 1 else 'BBB'
            bar = {'t': '2026-09-21T04:00:00Z', 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 20}
            return httpx.Response(200, json={'bars': {symbol: [bar]},
                                           'next_page_token': 'second' if len(calls) == 1 else None})
        provider = self.provider(handle)
        rows = provider.history(['AAA', 'BBB'], start=date(2026, 9, 21), end=date(2026, 9, 22))
        self.assertEqual(calls[1].url.params['page_token'], 'second')
        self.assertEqual(rows['BBB'][0]['timestamp'], '2026-09-21T00:00:00')
        self.assertEqual(rows['AAA'][0]['close'], 11)
        self.assertEqual(rows['AAA'][0]['source_provider'], 'alpaca-sip')
        self.assertNotIn('fixture-secret', json.dumps(provider.receipts))
        self.assertEqual(len(provider.source_documents), 2)
        for receipt in provider.receipts:
            self.assertIn(receipt['response_sha256'], provider.source_documents)

    def test_minute_start_timestamp_and_no_synthetic_missing_minute(self):
        provider = self.provider(lambda r: httpx.Response(200, json={'bars': {'AAA': [
            {'t': '2026-09-21T13:32:00Z', 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 3}
        ]}, 'next_page_token': None}))
        rows = provider.history(['AAA'], start=date(2026, 9, 21), end=date(2026, 9, 22), interval='1m')
        self.assertEqual([r['timestamp'] for r in rows['AAA']], ['2026-09-21T09:32:00-04:00'])

    def test_auth_failure_has_no_retry_or_secret_echo(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(403, json={'message': 'fixture-secret forbidden'})
        provider = self.provider(handle)
        with self.assertRaises(DataProviderError) as error:
            provider.history(['AAA'], start=date(2026, 9, 21), end=date(2026, 9, 22))
        self.assertEqual(len(calls), 1)
        self.assertEqual(error.exception.diagnostic['kind'], 'auth_error')
        self.assertNotIn('fixture-secret', str(error.exception))

    def test_incomplete_pagination_and_invalid_ohlc_are_refused(self):
        fixtures = [
            {'bars': {}, 'next_page_token': 'loop'},
            {'bars': {'AAA': [{'t': '2026-09-21T04:00:00Z', 'o': 10, 'h': 9, 'l': 11, 'c': 10, 'v': 3}]}},
            {'bars': {'AAA': [{'t': '2026-09-23T04:00:00Z', 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 3}]}},
        ]
        for body in fixtures:
            with self.subTest(body=body):
                provider = self.provider(lambda r: httpx.Response(200, json=body))
                with self.assertRaises(DataProviderError):
                    provider.history(['AAA'], start=date(2026, 9, 21), end=date(2026, 9, 22))

    def test_current_session_not_silently_replaced_with_delayed_feed(self):
        calls = []
        provider = self.provider(lambda r: calls.append(r))
        with self.assertRaises(DataProviderError):
            provider.history(['AAA'], start=date(2026, 9, 22), end=date(2026, 9, 23), interval='1m')
        self.assertEqual(calls, [])

    def test_no_retry_for_rate_limit_and_no_following_redirect_with_credentials(self):
        for status in [429, 302]:
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(status, headers={'Location': 'https://untrusted.invalid/'})
            with self.subTest(status=status):
                with self.assertRaises(DataProviderError):
                    self.provider(handle).history(['AAA'], start=date(2026, 9, 21), end=date(2026, 9, 22))
                self.assertEqual(len(calls), 1)

    def test_empty_symbol_and_exclusive_end_are_preserved_as_missing(self):
        provider = self.provider(lambda r: httpx.Response(200, json={'bars': {'AAA': [
            {'t': '2026-09-22T04:00:00Z', 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 3}
        ]}, 'next_page_token': None}))
        self.assertEqual(provider.history(['AAA', 'BBB'], start=date(2026, 9, 21),
                                          end=date(2026, 9, 22)), {'AAA': [], 'BBB': []})


class HistoricalFallbackTests(unittest.TestCase):
    def wrapper(self, primary, backup):
        from shaq_daily_oracle.history_fallback import HistoricalFallbackProvider
        return HistoricalFallbackProvider(primary, backup)

    def test_fallback_requests_only_missing_symbols_and_keeps_completed_prices(self):
        primary, backup = Mock(), Mock()
        primary.history.side_effect = DataProviderError('limited', diagnostic={'kind': 'rate_limited'})
        primary.recover_history.return_value = {'AAA': [{'timestamp': '2026-09-18', 'close': 10}]}
        backup.history.return_value = {'BBB': [{'timestamp': '2026-09-18', 'close': 20}]}
        provider = self.wrapper(primary, backup)
        result = provider.history(['AAA', 'BBB'], start=date(2026, 9, 1), end=date(2026, 9, 21))
        self.assertEqual(result['AAA'][0]['close'], 10)
        self.assertEqual(result['BBB'][0]['close'], 20)
        self.assertEqual(backup.history.call_args.args[0], ['BBB'])
        # The circuit remains open for this collection, not every future run.
        provider.history(['CCC'], start=date(2026, 9, 1), end=date(2026, 9, 21))
        self.assertEqual(primary.history.call_count, 1)

    def test_auth_and_malformed_primary_results_do_not_trigger_fallback(self):
        for kind in ['auth_error', 'protocol_error']:
            primary, backup = Mock(), Mock()
            primary.history.side_effect = DataProviderError('failed', diagnostic={'kind': kind})
            with self.assertRaises(DataProviderError):
                self.wrapper(primary, backup).history(['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 21))
            backup.history.assert_not_called()

    def test_premarket_and_options_do_not_use_historical_backup(self):
        primary, backup = Mock(), Mock()
        primary.recent_intraday.side_effect = DataProviderError('unavailable')
        primary.option_surface.return_value = {'status': 'no_data'}
        provider = self.wrapper(primary, backup)
        with self.assertRaises(DataProviderError):
            provider.recent_intraday(['AAA'], cutoff=datetime.fromisoformat('2026-09-22T08:30:00-04:00'))
        self.assertEqual(provider.option_surface('AAA'), {'status': 'no_data'})
        backup.history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
