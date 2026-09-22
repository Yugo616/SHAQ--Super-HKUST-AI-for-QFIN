import bz2
import unittest
from unittest.mock import patch

import httpx

from shaq_daily_oracle.data_providers import FinanceDatabaseProvider, DataProviderError


class MetadataTransportTests(unittest.TestCase):
    def test_metadata_uses_bounded_fetch_and_parses_compressed_source(self):
        observed = []
        payload = bz2.compress(b'symbol,name,sector\nAAA,Example,Technology\n')
        def fetch(url, **kwargs):
            observed.append(kwargs['timeout'])
            return httpx.Response(200, content=payload, request=httpx.Request('GET', url))
        with patch('httpx.get', side_effect=fetch):
            rows = FinanceDatabaseProvider(timeout_seconds=2).metadata(['AAA'])
        self.assertEqual(observed, [2])
        self.assertEqual(rows['AAA']['sector'], 'Technology')

    def test_timeout_is_reported_without_private_response_or_fake_metadata(self):
        with patch('httpx.get', side_effect=httpx.ReadTimeout('private transport detail')):
            with self.assertRaises(DataProviderError) as caught:
                FinanceDatabaseProvider(timeout_seconds=2).metadata(['AAA'])
        self.assertNotIn('private transport detail', str(caught.exception))

