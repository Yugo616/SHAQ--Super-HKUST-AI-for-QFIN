import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_research_lab_foundation as foundation
from shaq_daily_oracle.research_settings import ResearchSettingsStore


class AlpacaSettingsTests(unittest.TestCase):
    def test_credentials_cannot_be_routed_to_another_origin(self):
        from shaq_daily_oracle.data_providers import DataProfile, DataProviderError
        for url in ('https://example.org', 'https://data.alpaca.markets.evil.test',
                    'https://user@data.alpaca.markets', 'http://data.alpaca.markets'):
            with self.subTest(url=url), self.assertRaises(DataProviderError):
                DataProfile.from_dict({'profile_id': 'test', 'universe_file': 'test.csv',
                                       'alpaca_data_base_url': url})

    def test_credentials_stay_out_of_json_and_status_does_not_read_keychain(self):
        saved = {}
        class Keyring:
            def set_password(self, service, key, value): saved[key] = value
            def get_password(self, service, key): return saved.get(key)
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(directory)))
            with patch.object(store, '_keyring', return_value=Keyring()):
                store.set_alpaca_credentials('fixture-key', 'fixture-secret')
                self.assertEqual(store.get_alpaca_credentials(), ('fixture-key','fixture-secret'))
            with patch.object(store, '_keyring', side_effect=AssertionError('status must not access secrets')):
                self.assertTrue(store.public_settings()['alpaca_credentials_saved'])
            text=store.paths.research_settings_file.read_text()
            self.assertNotIn('fixture-key',text)
            self.assertNotIn('fixture-secret',text)
