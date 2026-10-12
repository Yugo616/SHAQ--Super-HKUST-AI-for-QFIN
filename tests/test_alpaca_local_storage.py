import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_research_lab_foundation as foundation
from shaq_daily_oracle.research_settings import ResearchSettingsStore
from shaq_daily_oracle.settings import SettingsError


class AlpacaLocalStorageTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(ResearchSettingsStore, '_keyring', side_effect=AssertionError('No Keychain allowed'))
        guard.start()
        self.addCleanup(guard.stop)

    def store(self, directory):
        return ResearchSettingsStore(foundation.ResearchLabFoundationTests().paths(Path(directory)))

    def test_save_restart_and_background_read_never_touch_keychain(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                ResearchSettingsStore, '_keyring', side_effect=AssertionError('No Keychain allowed')):
            store = self.store(directory)
            store.set_alpaca_credentials('fixture-key', 'fixture-secret')
            reopened = self.store(directory)
            self.assertEqual(reopened.get_alpaca_credentials(), ('fixture-key', 'fixture-secret'))
            self.assertTrue(reopened.public_settings()['alpaca_credentials_saved'])
            self.assertNotIn('fixture-secret', json.dumps(reopened.public_settings()))
            self.assertNotIn('fixture-secret', reopened.paths.research_settings_file.read_text())
            if os.name != 'nt':
                self.assertEqual(reopened.alpaca_credentials_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(reopened.alpaca_credentials_path.parent.stat().st_mode & 0o777, 0o700)

    def test_legacy_saved_marker_does_not_trigger_password_prompt_or_claim_ready(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                ResearchSettingsStore, '_keyring', side_effect=AssertionError('No Keychain allowed')):
            store = self.store(directory)
            store._record_secret_state('alpaca_credentials_saved', present=True)
            self.assertFalse(store.public_settings()['alpaca_credentials_saved'])
            with self.assertRaisesRegex(SettingsError, '填写'):
                store.get_alpaca_credentials()

    def test_corrupt_or_partial_file_is_not_treated_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            store.set_alpaca_credentials('fixture-key', 'fixture-secret')
            for content in ('not-json private-fixture', '{"key_id":"fixture"}', '{"key_id":3,"secret_key":[]}'):
                store.alpaca_credentials_path.write_text(content)
                with self.assertRaises(SettingsError) as caught:
                    store.get_alpaca_credentials()
                self.assertNotIn('private-fixture', str(caught.exception))

    def test_failed_atomic_replacement_preserves_existing_key_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            store.set_alpaca_credentials('old-key', 'old-secret')
            with patch('os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    store.set_alpaca_credentials('new-key', 'new-secret')
            self.assertEqual(store.get_alpaca_credentials(), ('old-key', 'old-secret'))
            self.assertEqual(len(list(store.alpaca_credentials_path.parent.iterdir())), 1)
