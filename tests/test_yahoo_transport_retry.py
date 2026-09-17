from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, PropertyMock

import pandas as pd
import yfinance as yf
from curl_cffi.curl import CurlError

from shaq_daily_oracle import collection_worker
from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider


class YahooTransportRetryTests(unittest.TestCase):
    def test_curl_timeout_code_survives_wrapper_without_private_text(self):
        try:
            try:
                raise CurlError('Authorization: private-fixture', code=28)
            except CurlError as exc:
                raise DataProviderError('private fixture wrapper') from exc
        except DataProviderError as exc:
            diagnostic = collection_worker._failure_diagnostic(exc, 'history')
        self.assertEqual(diagnostic.get('kind'), 'timeout')
        self.assertEqual(diagnostic.get('curl_code'), 28)
        self.assertNotIn('private', json.dumps(diagnostic))

    def test_transport_classification_does_not_retry_auth_schema_or_resource_errors(self):
        from yfinance.exceptions import YFRateLimitError, YFPricesMissingError
        cases = [
            (CurlError('private', code=7), 'connection_error', True),
            (TimeoutError('private'), 'timeout', True),
            (YFRateLimitError(), 'rate_limited', True),
            (OSError(24, 'private'), 'resource_exhausted', False),
            (ValueError('private'), 'provider_error', False),
            (YFPricesMissingError('FIX', 'private'), 'no_data', False),
        ]
        for status, kind, retry in [(429, 'rate_limited', True), (503, 'provider_unavailable', True),
                                    (403, 'auth_error', False), (404, 'provider_error', False)]:
            error = RuntimeError('private')
            error.response = SimpleNamespace(status_code=status, headers={'private': 'secret'})
            cases.append((error, kind, retry))
        for exc, kind, retry in cases:
            with self.subTest(kind=kind, exception=type(exc).__name__):
                diagnostic = collection_worker._failure_diagnostic(exc, 'history')
                self.assertEqual(diagnostic.get('kind'), kind)
                self.assertEqual(diagnostic.get('retryable'), retry)
                self.assertNotIn('private', json.dumps(diagnostic))

    def test_parent_preserves_safe_transport_diagnostic_and_chinese_message(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'error': 'private', 'diagnostic': {
            'kind': 'timeout', 'stage': 'history', 'curl_code': 28, 'retryable': True,
            'attempts': 2, 'error_type': 'CurlError', 'headers': {'Authorization': 'private'},
            'http_status': 'private', 'errno': 'private'}}), '')
        with patch.object(collection_worker, 'run_model_process', return_value=result):
            with self.assertRaises(DataProviderError) as caught:
                YFinanceProvider(DataProfile('test', 'unused')).history(
                    ['FIX'], start=date(2026, 9, 1), end=date(2026, 9, 2))
        self.assertEqual(caught.exception.diagnostic.get('curl_code'), 28)
        self.assertEqual(caught.exception.diagnostic.get('attempts'), 2)
        self.assertIn('超时', str(caught.exception))
        self.assertNotIn('private', str(caught.exception) + json.dumps(caught.exception.diagnostic))

    def test_history_retries_only_failed_ticker_and_returns_original_successful_rows(self):
        calls = []
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            if ticker.ticker == 'BBB' and calls.count('BBB') == 1:
                raise CurlError('private', code=28)
            return pd.DataFrame({'Open': [101 if ticker.ticker == 'AAA' else 202], 'Volume': [3]},
                                index=pd.DatetimeIndex(['2026-09-01'], tz='UTC'))
        with patch.object(yf.Ticker, 'history', history):
            rows = YFinanceProvider(DataProfile('test', 'unused'))._history_inline(
                ['AAA', 'BBB'], start=date(2026, 9, 1), end=date(2026, 9, 2), session=None)
        self.assertEqual(calls, ['AAA', 'BBB', 'BBB'])
        self.assertEqual(rows['AAA'][0]['open'], 101)
        self.assertEqual(rows['BBB'][0]['open'], 202)

    def test_repeated_timeout_is_bounded_and_never_an_empty_success(self):
        calls = []
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            raise CurlError('private', code=28)
        with patch.object(yf.Ticker, 'history', history):
            with self.assertRaises(DataProviderError) as caught:
                YFinanceProvider(DataProfile('test', 'unused'))._history_inline(
                    ['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 2), session=None)
        self.assertEqual(calls, ['AAA', 'AAA'])
        self.assertEqual(caught.exception.diagnostic.get('attempts'), 2)
        self.assertNotIn('private', str(caught.exception))

    def test_auth_failure_is_not_retried(self):
        calls = []
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            error = RuntimeError('private')
            error.response = SimpleNamespace(status_code=403)
            raise error
        with patch.object(yf.Ticker, 'history', history):
            with self.assertRaises(DataProviderError) as caught:
                YFinanceProvider(DataProfile('test', 'unused'))._history_inline(
                    ['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 2), session=None)
        self.assertEqual(calls, ['AAA'])
        self.assertEqual(caught.exception.diagnostic.get('kind'), 'auth_error')

    def test_retry_runtime_policy_does_not_change_source_identity_and_rejects_unbounded_input(self):
        profile = DataProfile('test', 'unused')
        changed = DataProfile.from_dict({**profile.source_dict(), 'yahoo_request_max_retries': 2,
                                         'yahoo_retry_backoff_seconds': 0})
        self.assertEqual(profile.identity(), changed.identity())
        for field, value in [('yahoo_request_max_retries', -1), ('yahoo_request_max_retries', 1000),
                             ('yahoo_request_max_retries', True), ('yahoo_retry_backoff_seconds', -1),
                             ('yahoo_retry_backoff_seconds', float('nan'))]:
            with self.subTest(field=field, value=value), self.assertRaises(DataProviderError):
                replace(changed, **{field: value}).validate()

    def test_option_requests_retry_only_failed_expiry(self):
        calls = []
        def chain(ticker, expiry):
            calls.append(expiry)
            if expiry == '2026-09-25' and calls.count(expiry) == 1:
                raise CurlError('private', code=7)
            return SimpleNamespace(calls=pd.DataFrame([{'strike': 100, 'bid': 2}]),
                                   puts=pd.DataFrame([{'strike': 100, 'bid': 3}]))
        with patch.object(yf.Ticker, 'options', new_callable=PropertyMock,
                          return_value=('2026-09-18', '2026-09-25')), \
             patch.object(yf.Ticker, 'fast_info', new_callable=PropertyMock,
                          return_value={'last_price': 100}), \
             patch.object(yf.Ticker, 'option_chain', chain):
            result = YFinanceProvider(DataProfile('test', 'unused'))._option_surface_inline('AAA', session=None)
        self.assertEqual(calls, ['2026-09-18', '2026-09-25', '2026-09-25'])
        self.assertEqual(result['expiries']['2026-09-18']['calls'][0]['bid'], 2)
        self.assertEqual(result['expiries']['2026-09-25']['puts'][0]['bid'], 3)

    def test_option_surface_separates_chain_coverage_from_quote_quality(self):
        trade_time = pd.Timestamp('2026-09-17T15:32:00Z')
        calls = pd.DataFrame([
            {'contractSymbol': 'AAA260918C00099000', 'strike': 99, 'bid': 1.0, 'ask': 1.2,
             'volume': 5, 'openInterest': 10, 'lastTradeDate': trade_time},
            {'contractSymbol': 'AAA260918C00100000', 'strike': 100, 'bid': 0, 'ask': 0,
             'volume': 0, 'openInterest': 3, 'lastTradeDate': trade_time},
            {'contractSymbol': 'AAA260918C00101000', 'strike': 101, 'bid': None, 'ask': None,
             'volume': 2, 'openInterest': 0, 'lastTradeDate': trade_time},
        ])
        puts = pd.DataFrame([
            {'contractSymbol': 'AAA260918P00099000', 'strike': 99, 'bid': 1.1, 'ask': 1.3,
             'volume': 0, 'openInterest': 8, 'lastTradeDate': trade_time},
            {'contractSymbol': 'AAA260918P00100000', 'strike': 100, 'bid': 0.8, 'ask': 0,
             'volume': 0, 'openInterest': 0, 'lastTradeDate': trade_time},
        ])
        with patch.object(yf.Ticker, 'options', new_callable=PropertyMock,
                          return_value=('2026-09-18',)), \
             patch.object(yf.Ticker, 'fast_info', new_callable=PropertyMock,
                          return_value={'last_price': 100}), \
             patch.object(yf.Ticker, 'option_chain',
                          return_value=SimpleNamespace(calls=calls, puts=puts)):
            result = YFinanceProvider(DataProfile(
                'test', 'unused', maximum_option_contracts_per_side=2,
            ))._option_surface_inline('AAA', session=None)

        expiry = result['expiries']['2026-09-18']
        self.assertEqual(expiry['quality'], {
            'source_contract_count': 5,
            'retained_contract_count': 4,
            'contracts_with_valid_two_sided_price': 2,
            'contracts_with_positive_volume': 1,
            'contracts_with_positive_open_interest': 3,
            'price_pair_status': 'partial',
            'quote_freshness_status': 'unverifiable',
            'contracts_with_fresh_quote': None,
        })
        self.assertEqual(result['quality'], expiry['quality'])
        self.assertEqual(result['available_expiry_count'], 1)
        self.assertEqual(result['collected_expiry_count'], 1)
        self.assertEqual(result['maximum_option_expiries'], 3)
        self.assertEqual(result['expiry_selection'], 'nearest_configured_expiries')
        self.assertEqual(expiry['calls'][0]['lastTradeDate'], '2026-09-17T15:32:00+00:00')
        self.assertIsNotNone(result['captured_at'])
        self.assertEqual(result['capture_timestamp_semantics'],
                         'provider_response_capture_not_exchange_quote_time')
        self.assertEqual(result['quote_timestamp_status'], 'unavailable')
        self.assertFalse(result['quote_freshness_eligible'])
        self.assertEqual(
            result['last_trade_timestamp_semantics'],
            'contract_last_trade_not_quote_time',
        )
        self.assertFalse(result['directional_flow_semantics'])

    def test_option_expiry_failure_exhaustion_preserves_classification(self):
        with patch.object(yf.Ticker, 'options', new_callable=PropertyMock,
                          side_effect=CurlError('private', code=28)):
            try:
                YFinanceProvider(DataProfile('test', 'unused'))._option_surface_inline('AAA', session=None)
            except Exception as exc:
                self.assertIsInstance(exc, DataProviderError)
                self.assertEqual(exc.diagnostic.get('kind'), 'timeout')
                self.assertEqual(exc.diagnostic.get('retry_count'), 1)
            else:
                self.fail('An exhausted option request must not become no_data')

    def test_option_surface_does_not_call_all_failed_expiries_collected(self):
        with patch.object(yf.Ticker, 'options', new_callable=PropertyMock,
                          return_value=('2026-09-18', '2026-09-25')), \
             patch.object(yf.Ticker, 'fast_info', new_callable=PropertyMock,
                          return_value={'last_price': 100}), \
             patch.object(yf.Ticker, 'option_chain', side_effect=ValueError('bad schema')):
            result = YFinanceProvider(DataProfile(
                'test', 'unused', yahoo_request_max_retries=0,
            ))._option_surface_inline('AAA', session=None)

        self.assertEqual(result['status'], 'provider_error')
        self.assertEqual(result['collected_expiry_count'], 0)
        self.assertEqual(result['failed_expiry_count'], 2)
        self.assertEqual(result['quality']['source_contract_count'], 0)

    def test_option_surface_reports_empty_chain_separately_from_quote_quality(self):
        with patch.object(yf.Ticker, 'options', new_callable=PropertyMock, return_value=()):
            result = YFinanceProvider(DataProfile(
                'test', 'unused',
            ))._option_surface_inline('AAA', session=None)

        self.assertEqual(result['status'], 'no_data')
        self.assertEqual(result['available_expiry_count'], 0)
        self.assertEqual(result['collected_expiry_count'], 0)
        self.assertEqual(result['quality'], {
            'source_contract_count': 0,
            'retained_contract_count': 0,
            'contracts_with_valid_two_sided_price': 0,
            'contracts_with_positive_volume': 0,
            'contracts_with_positive_open_interest': 0,
            'price_pair_status': 'unavailable',
            'quote_freshness_status': 'unverifiable',
            'contracts_with_fresh_quote': None,
        })
        self.assertEqual(result['capture_timestamp_semantics'],
                         'provider_response_capture_not_exchange_quote_time')
        self.assertEqual(result['quote_timestamp_status'], 'unavailable')
        self.assertFalse(result['quote_freshness_eligible'])

    def test_real_worker_protocol_reports_exhausted_retry_without_private_text(self):
        import sys
        script = r'''
from unittest.mock import patch
import yfinance as yf
from curl_cffi.curl import CurlError
from shaq_daily_oracle.collection_worker import main
def history(ticker, **kwargs):
    raise CurlError('Authorization: private-fixture', code=28)
with patch.object(yf.Ticker, 'history', history): raise SystemExit(main())
'''
        request = {'operation': 'history', 'payload': {
            'profile': {'profile_id': 'test', 'universe_file': 'unused',
                        'yahoo_request_max_retries': 2, 'yahoo_retry_backoff_seconds': 0},
            'symbols': ['FIX'], 'start': '2026-09-01', 'end': '2026-09-02'}}
        completed = subprocess.run([sys.executable, '-c', script], input=json.dumps(request),
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertNotIn('result', envelope)
        self.assertEqual(envelope['diagnostic']['curl_code'], 28)
        self.assertEqual(envelope['diagnostic']['retry_count'], 2)
        self.assertNotIn('private', completed.stdout)


if __name__ == '__main__':
    unittest.main()
