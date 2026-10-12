import unittest


class AlpacaSettingsTests(unittest.TestCase):
    def test_credentials_cannot_be_routed_to_another_origin(self):
        from shaq_daily_oracle.data_providers import DataProfile, DataProviderError
        for url in ('https://example.org', 'https://data.alpaca.markets.evil.test',
                    'https://user@data.alpaca.markets', 'http://data.alpaca.markets'):
            with self.subTest(url=url), self.assertRaises(DataProviderError):
                DataProfile.from_dict({'profile_id': 'test', 'universe_file': 'test.csv',
                                       'alpaca_data_base_url': url})
