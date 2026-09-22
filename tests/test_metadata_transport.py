import bz2
import unittest
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from shaq_daily_oracle.data_providers import FinanceDatabaseProvider, DataProviderError


class MetadataTransportTests(unittest.TestCase):
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
