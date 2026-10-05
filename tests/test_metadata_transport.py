import bz2
import unittest
import os
import threading
import time
import tempfile
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from shaq_daily_oracle.data_providers import FinanceDatabaseProvider, DataProviderError, SecEdgarProvider


class MetadataTransportTests(unittest.TestCase):
    def test_sec_transient_handshake_retries_and_populates_verified_cache(self):
        from types import SimpleNamespace
        payload = {'fields': ['cik', 'name', 'ticker', 'exchange'],
                   'data': [[320193, 'Apple Inc.', 'AAPL', 'Nasdaq']]}
        response = httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://www.sec.gov'))
        with tempfile.TemporaryDirectory() as directory:
            provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2,
                                       cache_root=Path(directory), retry_backoff_seconds=0)
            members = [SimpleNamespace(symbol='AAPL', cik='0000320193')]
            with patch('curl_cffi.requests.Session.get', side_effect=[ConnectionError(), response]):
                rows = provider.instrument_metadata(members)
            self.assertEqual(rows['AAPL']['name'], 'Apple Inc.')
            saved = provider.metadata_receipt.copy()
            with patch('curl_cffi.requests.Session.get', side_effect=AssertionError('fresh cache should avoid network')):
                self.assertEqual(provider.instrument_metadata(members), rows)
            self.assertEqual(provider.metadata_receipt['fetched_at'], saved['fetched_at'])
            self.assertEqual(provider.metadata_receipt['cache_status'], 'cached')

    def test_sec_stale_cache_survives_transient_failure_but_not_corruption(self):
        from types import SimpleNamespace
        payload = {'fields': ['cik', 'name', 'ticker', 'exchange'],
                   'data': [[320193, 'Apple Inc.', 'AAPL', 'Nasdaq']]}
        response = httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://www.sec.gov'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2,
                                       cache_root=root, cache_max_age_seconds=0, retry_backoff_seconds=0)
            members = [SimpleNamespace(symbol='AAPL', cik='0000320193')]
            with patch('curl_cffi.requests.Session.get', return_value=response):
                rows = provider.instrument_metadata(members)
            fetched = provider.metadata_receipt['fetched_at']
            with patch('curl_cffi.requests.Session.get', side_effect=ConnectionError()):
                self.assertEqual(provider.instrument_metadata(members), rows)
            self.assertEqual(provider.metadata_receipt['fetched_at'], fetched)
            self.assertEqual(provider.metadata_receipt['cache_status'], 'cached_after_refresh_failure')
            for path in root.glob('*.json'):
                if path.name != 'source.json': path.write_text('{}')
            with patch('curl_cffi.requests.Session.get', side_effect=ConnectionError()):
                with self.assertRaises(DataProviderError): provider.instrument_metadata(members)

    def test_sec_access_denied_is_not_retried(self):
        response = httpx.Response(403, request=httpx.Request('GET', 'https://www.sec.gov'))
        def fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            return response
        calls = 0
        provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2,
                                   retry_backoff_seconds=0)
        with patch('curl_cffi.requests.Session.get', fetch):
            with self.assertRaises(DataProviderError) as caught: provider.instrument_metadata([object()])
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.diagnostic['http_status'], 403)

    def test_sec_identity_fallback_matches_ticker_and_cik_not_fuzzy_name(self):
        from types import SimpleNamespace
        payload = {'fields': ['cik', 'name', 'ticker', 'exchange'], 'data': [
            [320193, 'Apple Inc.', 'AAPL', 'Nasdaq'],
            [1, 'Wrong issuer', 'ORCL', 'NYSE']]}
        provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2)
        with patch('curl_cffi.requests.Session.get', return_value=httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://www.sec.gov'))):
            rows = provider.instrument_metadata([
                SimpleNamespace(symbol='AAPL', cik='0000320193'),
                SimpleNamespace(symbol='ORCL', cik='0001341439')])
        self.assertEqual(set(rows), {'AAPL'})
        self.assertEqual(rows['AAPL']['exchange'], 'Nasdaq')
        self.assertNotIn('sector', rows['AAPL'])
        self.assertEqual(provider.metadata_receipt['provider'], 'sec-edgar')

    def test_verified_cache_reuses_source_and_conditional_refresh(self):
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        with tempfile.TemporaryDirectory() as directory:
            provider = FinanceDatabaseProvider(timeout_seconds=2, cache_root=Path(directory))
            def fetch(session, url, **kwargs):
                if kwargs.get('headers', {}).get('If-None-Match') == '"fixture"':
                    return httpx.Response(304, request=httpx.Request('GET', url))
                return httpx.Response(200, content=payload, headers={'ETag': '"fixture"'}, request=httpx.Request('GET', url))
            with patch('curl_cffi.requests.Session.get', fetch):
                first = provider.metadata(['AAA'])
                second = provider.metadata(['AAA'])
            self.assertEqual(first, second)
            self.assertEqual(second['AAA']['sector'], 'Technology')
            self.assertEqual(provider.receipt['cache_status'], 'revalidated')
            self.assertIn('fetched_at', provider.receipt)

    def test_transient_failure_preserves_verified_cache_not_freshness(self):
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        with tempfile.TemporaryDirectory() as directory:
            provider = FinanceDatabaseProvider(timeout_seconds=2, cache_root=Path(directory))
            response = httpx.Response(200, content=payload, request=httpx.Request('GET', provider.equities_dataset_url))
            with patch('curl_cffi.requests.Session.get', return_value=response):
                provider.metadata(['AAA'])
            original_time = provider.receipt['fetched_at']
            with patch('curl_cffi.requests.Session.get', side_effect=TimeoutError('private')):
                rows = provider.metadata(['AAA'])
            self.assertEqual(rows['AAA']['name'], 'Example')
            self.assertEqual(provider.receipt['cache_status'], 'cached_after_refresh_failure')
            self.assertEqual(provider.receipt['fetched_at'], original_time)
            self.assertEqual(provider.receipt['refresh_failure']['kind'], 'timeout')

    def test_corrupt_cache_cannot_be_used_on_network_failure(self):
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FinanceDatabaseProvider(timeout_seconds=2, cache_root=root)
            with patch('curl_cffi.requests.Session.get', return_value=httpx.Response(200, content=payload, request=httpx.Request('GET', provider.equities_dataset_url))):
                provider.metadata(['AAA'])
            next(root.glob('*.bz2')).write_bytes(b'corrupt')
            with patch('curl_cffi.requests.Session.get', side_effect=TimeoutError()):
                with self.assertRaises(DataProviderError):
                    provider.metadata(['AAA'])

    def test_empty_request_does_not_download_world_database(self):
        with patch('curl_cffi.requests.Session.get', side_effect=AssertionError('unexpected download')):
            self.assertEqual(FinanceDatabaseProvider(timeout_seconds=2).metadata([]), {})

    def test_slow_trickling_metadata_cannot_extend_total_download_deadline(self):
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    for offset in range(0, len(payload), 3):
                        self.wfile.write(payload[offset:offset+3])
                        self.wfile.flush()
                        time.sleep(.04)
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = FinanceDatabaseProvider(timeout_seconds=.15)
        provider.equities_dataset_url = f'http://127.0.0.1:{server.server_port}/equities.bz2'
        try:
            with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1'}):
                with self.assertRaises(DataProviderError):
                    provider.metadata(['AAA'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_metadata_uses_bounded_fetch_and_parses_compressed_source(self):
        observed = []
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        def fetch(session, url, **kwargs):
            observed.append(kwargs['timeout'])
            return httpx.Response(200, content=payload, request=httpx.Request('GET', url))
        with patch('curl_cffi.requests.Session.get', fetch):
            rows = FinanceDatabaseProvider(timeout_seconds=2).metadata(['AAA'])
        self.assertEqual(observed, [2])
        self.assertEqual(rows['AAA']['sector'], 'Technology')

    def test_timeout_is_reported_without_private_response_or_fake_metadata(self):
        with patch('curl_cffi.requests.Session.get', side_effect=httpx.ReadTimeout('private transport detail')):
            with self.assertRaises(DataProviderError) as caught:
                FinanceDatabaseProvider(timeout_seconds=2).metadata(['AAA'])
        self.assertNotIn('private transport detail', str(caught.exception))
