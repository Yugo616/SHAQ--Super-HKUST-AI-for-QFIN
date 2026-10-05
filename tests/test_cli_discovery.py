import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from shaq_daily_oracle import model_backends


class CliDiscoveryTests(unittest.TestCase):
    def test_native_window_finds_nested_chatgpt_cli_even_if_app_was_renamed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bundle = root / 'My AI App.app' / 'Contents'
            nested = bundle / 'Resources/codex-cli/CodexCLI.app/Contents'
            (nested / 'MacOS').mkdir(parents=True)
            (bundle / 'Info.plist').write_bytes(plistlib.dumps(
                {'CFBundleIdentifier': 'com.openai.codex'}))
            (nested / 'Info.plist').write_bytes(plistlib.dumps(
                {'CFBundleExecutable': 'codex'}))
            cli = nested / 'MacOS/codex'
            cli.write_text('#!/bin/sh\nexit 0\n')
            cli.chmod(0o700)
            self.assertEqual(model_backends.find_desktop_cli('codex', [root]), str(cli))
            cli.chmod(0o600)
            if os.name == 'nt':
                # Windows chmod does not control POSIX execute bits. Exercise
                # the same denied-access boundary without assuming Unix modes.
                with patch.object(model_backends.os, 'access', return_value=False):
                    self.assertIsNone(model_backends.find_desktop_cli('codex', [root]))
            else:
                self.assertIsNone(model_backends.find_desktop_cli('codex', [root]))

    def test_nested_cli_is_not_taken_from_an_unrelated_app(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bundle = root / 'ChatGPT.app/Contents'
            cli = bundle / 'Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            cli.parent.mkdir(parents=True)
            cli.write_text('#!/bin/sh\nexit 0\n')
            cli.chmod(0o700)
            (bundle / 'Info.plist').write_bytes(plistlib.dumps(
                {'CFBundleIdentifier': 'unrelated.app'}))
            self.assertIsNone(model_backends.find_desktop_cli('codex', [root]))

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
