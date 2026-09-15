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
