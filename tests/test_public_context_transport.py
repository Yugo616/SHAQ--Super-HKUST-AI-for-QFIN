import os
import builtins
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from shaq_daily_oracle.public_data import collect_public_context


class PublicContextTransportTests(unittest.TestCase):
    def test_optional_transport_missing_is_a_provider_failure(self):
        original_import = builtins.__import__

        def without_transport(name, *args, **kwargs):
            if name.startswith('curl_cffi'):
                raise ModuleNotFoundError('optional transport unavailable')
            return original_import(name, *args, **kwargs)

        config = {'timeout_seconds': 2, 'sources': {
            'sample': {'url': 'https://example.test/data', 'kind': 'csv', 'enabled': True}}}
        with patch('builtins.__import__', side_effect=without_transport):
            packet = collect_public_context(config, cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc))[0]
        self.assertEqual(packet['status'], 'provider_error')

    def test_slow_response_cannot_keep_optional_collection_waiting(self):
        payload = b'DATE,CLOSE\n01/02/2025,15.0\n'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    for offset in range(0, len(payload), 2):
                        self.wfile.write(payload[offset:offset+2])
                        self.wfile.flush()
                        time.sleep(.04)
                except OSError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {'timeout_seconds': .15, 'sources': {
            'slow': {'url': f'http://127.0.0.1:{server.server_port}/data',
                     'kind': 'csv', 'enabled': True},
            'disabled': {'url': 'https://example.test/unused', 'enabled': False}}}
        try:
            with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1'}):
                packets = collect_public_context(config, cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(packets[0]['status'], 'provider_error')
            self.assertNotIn('raw', packets[0])
            self.assertEqual(packets[1]['status'], 'not_enabled')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_success_preserves_source_bytes_and_cutoff_filtering(self):
        payload = b'DATE,CLOSE\n01/02/2025,15.0\n01/01/2030,99.0\n'
        config = {'timeout_seconds': 2, 'sources': {
            'sample': {'url': 'https://example.test/data', 'kind': 'csv', 'enabled': True}}}
        response = httpx.Response(200, content=payload,
                                  request=httpx.Request('GET', 'https://example.test/data'))
        with patch('curl_cffi.requests.Session.get', return_value=response) as fetch:
            packet = collect_public_context(config, cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc))[0]
        self.assertEqual(fetch.call_args.kwargs['timeout'], 2)
        self.assertIs(fetch.call_args.kwargs['stream'], False)
        self.assertEqual(packet['status'], 'collected')
        self.assertEqual(packet['data'], [{'DATE': '01/02/2025', 'CLOSE': '15.0'}])
        self.assertEqual(packet['raw'], payload)

    def test_late_fetch_stays_after_cutoff(self):
        config = {'timeout_seconds': 2, 'sources': {
            'sample': {'url': 'https://example.test/data', 'kind': 'html', 'enabled': True}}}
        response = httpx.Response(200, text='<table><tr><td>Rates</td></tr></table>',
                                  request=httpx.Request('GET', 'https://example.test/data'))
        with patch('curl_cffi.requests.Session.get', return_value=response):
            packet = collect_public_context(config, cutoff=datetime(2020, 1, 1, tzinfo=timezone.utc))[0]
        self.assertEqual(packet['status'], 'after_cutoff')
