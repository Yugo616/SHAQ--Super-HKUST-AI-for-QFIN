import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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

    def test_windows_cli_environment_keeps_system_variables_but_no_secrets(self):
        environment = {
            'PATH': r'C:\bin', 'SYSTEMROOT': r'C:\WINDOWS', 'USERPROFILE': r'C:\Users\member',
            'APPDATA': r'C:\Users\member\AppData\Roaming',
            'OPENAI_API_KEY': 'secret-value', 'SHAQ_EVIDENCE_PACKET': 'frozen-evidence',
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(model_backends, '_IS_WINDOWS', True),
        ):
            passed = model_backends._local_cli_environment()
        for key in ('PATH', 'SYSTEMROOT', 'USERPROFILE', 'APPDATA'):
            self.assertEqual(passed[key], environment[key])
        self.assertNotIn('OPENAI_API_KEY', passed)
        self.assertNotIn('SHAQ_EVIDENCE_PACKET', passed)

    def test_posix_cli_environment_drops_windows_variables(self):
        environment = {'PATH': '/usr/bin', 'HOME': '/Users/member', 'SYSTEMROOT': r'C:\WINDOWS'}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(model_backends, '_IS_WINDOWS', False),
        ):
            passed = model_backends._local_cli_environment()
        self.assertEqual(passed, {'PATH': '/usr/bin', 'HOME': '/Users/member'})

    def test_windows_discovery_skips_npm_posix_script_for_cmd_shim(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            script = root / 'claude'
            script.write_text('#!/bin/sh\nexit 0\n')
            shim = root / 'claude.cmd'
            shim.write_text('@echo off\n')
            for launcher in (script, shim):
                launcher.chmod(0o700)
            with patch.object(model_backends, '_IS_WINDOWS', True):
                self.assertEqual(model_backends.find_desktop_cli('claude', [root]), str(shim))
            with patch.object(model_backends, '_IS_WINDOWS', False):
                self.assertEqual(model_backends.find_desktop_cli('claude', [root]), str(script))

    def test_windows_discovery_finds_native_installer_executable(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            native = root / 'claude.exe'
            native.write_bytes(b'MZ')
            native.chmod(0o700)
            with patch.object(model_backends, '_IS_WINDOWS', True):
                self.assertEqual(model_backends.find_desktop_cli('claude', [root]), str(native))
