import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from shaq_daily_oracle import model_backends


class CliDiscoveryTests(unittest.TestCase):
    def test_native_window_can_find_official_app_cli_without_terminal_path(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bundle = root / 'Renamed App.app' / 'Contents'
            (bundle / 'Resources').mkdir(parents=True)
            (bundle / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex'}))
            cli = bundle / 'Resources' / 'codex'
            cli.write_text('#!/bin/sh\nexit 0\n')
            cli.chmod(0o700)
            discover = getattr(model_backends, 'find_desktop_cli', None)
            self.assertIsNotNone(discover)
            self.assertEqual(discover('codex', [root]), str(cli))
            (bundle / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'unknown.app'}))
            self.assertIsNone(discover('codex', [root]))
