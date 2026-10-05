import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from shaq_daily_oracle.data_providers import SecEdgarProvider, sec_exhibit_urls


class SecExhibitTests(unittest.TestCase):
    base = 'https://www.sec.gov/Archives/edgar/data/123/00012326000001/report.htm'

    def test_generic_exhibit_99_and_991_same_accession_only(self):
        html = b'''<table><tr><td>99</td><td><a href="release.htm">News release</a></td></tr>
        <tr><td>99.1</td><td><a href="results.htm">Quarterly financial results</a></td></tr></table>
        <a href="release.htm">Exhibit 99</a><a href="https://outside.test/a.htm">Exhibit 99</a>
        <a href="../different/release.htm">Exhibit 99</a><a href="%2e%2e/release.htm">Exhibit 99</a>
        <a href="/Archives/edgar/data/123/older/release.htm">Exhibit 99</a>
        <a href="logo.jpg">Exhibit 99</a>'''
        self.assertEqual(sec_exhibit_urls(self.base, html), [
            self.base.replace('report.htm', 'release.htm'),
            self.base.replace('report.htm', 'results.htm')])

    def test_label_split_across_tags_and_fragment_deduplicated(self):
        html = b'<tr><td><span>99.2</span></td><td><a href="a.htm#x"><b>Presentation</b></a></td></tr><a href="a.htm">Exhibit 99.2</a>'
        self.assertEqual(sec_exhibit_urls(self.base, html), [self.base.replace('report.htm','a.htm')])

    def test_unrelated_same_filing_links_are_not_earnings_exhibits(self):
        self.assertEqual(sec_exhibit_urls(self.base, b'<a href="charter.htm">Charter</a><a href="#item99">Item 99</a>'), [])

    def test_missing_acceptance_time_is_not_invented_from_filing_date(self):
        provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2)
        body = {'filings': {'recent': {'form': ['8-K'], 'filingDate': ['2026-10-01'],
                'accessionNumber': ['000123-26-000001'], 'primaryDocument': ['report.htm']}}}
        response = httpx.Response(200, json=body, request=httpx.Request('GET', self.base))
        with patch('httpx.get', return_value=response):
            result = provider.recent_events([SimpleNamespace(symbol='TEST', cik='0000000123')],
                since=datetime(2026,9,30,tzinfo=timezone.utc), cutoff=datetime(2026,10,1,12,50,tzinfo=timezone.utc))
        self.assertNotIn('collected', [row['status'] for row in result['TEST']])
        self.assertEqual(result['TEST'][0]['reason'], 'missing_acceptance_timestamp')

    def test_primary_download_retries_transient_not_permission_errors(self):
        provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2,
                                   retry_backoff_seconds=0)
        response = httpx.Response(200, content=b'<html>earnings</html>', request=httpx.Request('GET', self.base))
        with patch('httpx.get', side_effect=[httpx.ReadTimeout('timeout'), response]) as fetch:
            self.assertEqual(provider.download_primary_document(self.base), response.content)
            self.assertEqual(fetch.call_count, 2)
        with patch('httpx.get', return_value=httpx.Response(403, request=httpx.Request('GET', self.base))) as fetch:
            with self.assertRaises(ValueError): provider.download_primary_document(self.base)
            self.assertEqual(fetch.call_count, 1)

    def test_proxy_transport_failure_uses_standard_transport_without_changing_identity(self):
        from unittest.mock import MagicMock
        provider = SecEdgarProvider(user_agent='Research test@example.edu', timeout_seconds=2,
                                   retry_backoff_seconds=0)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'<html>earnings</html>'
        with patch('httpx.get', side_effect=httpx.ProxyError('transport negotiation failed')), \
             patch('urllib.request.urlopen', return_value=response) as fallback:
            self.assertEqual(provider.download_primary_document(self.base), b'<html>earnings</html>')
        request = fallback.call_args.args[0]
        self.assertEqual(request.full_url, self.base)
        self.assertEqual(request.get_header('User-agent'), provider.user_agent)
