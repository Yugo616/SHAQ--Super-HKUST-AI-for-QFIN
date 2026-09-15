import json
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.settings import _atomic_json as _settings_atomic_json

_atomic_json = partial(_settings_atomic_json, retry_windows_readers=True)


class SettingsPublicationTests(unittest.TestCase):
    def test_temporary_windows_read_denial_is_retried(self):
        from shaq_daily_oracle import settings
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            _atomic_json(path, {'status': 'running'})
            replace = settings.os.replace
            error = PermissionError('reader holds the destination')
            error.winerror = 5
            calls = []
            def locked(source, target):
                calls.append(1)
                if len(calls) < 3:
                    raise error
                return replace(source, target)
            with patch.object(settings.sys, 'platform', 'win32'), \
                 patch.object(settings.os, 'replace', side_effect=locked), \
                 patch.object(settings.time, 'sleep'):
                _atomic_json(path, {'status': 'complete'})
            self.assertEqual(len(calls), 3)
            self.assertEqual(json.loads(path.read_text())['status'], 'complete')

    def test_permanent_denial_is_bounded_and_preserves_old_document(self):
        from shaq_daily_oracle import settings
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            _atomic_json(path, {'status': 'running'})
            before = path.read_bytes()
            error = PermissionError('denied')
            error.winerror = 5
            with patch.object(settings.sys, 'platform', 'win32'), \
                 patch.object(settings.os, 'replace', side_effect=error) as replace, \
                 patch.object(settings.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    _atomic_json(path, {'status': 'complete'})
            self.assertEqual(replace.call_count, len(settings.WINDOWS_REPLACE_RETRY_DELAYS) + 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_non_windows_error_is_not_retried(self):
        from shaq_daily_oracle import settings
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(settings.sys, 'platform', 'darwin'), \
                 patch.object(settings.os, 'replace', side_effect=PermissionError('denied')) as replace:
                with self.assertRaises(PermissionError):
                    _atomic_json(Path(directory) / 'status.json', {'status': 'complete'})
            self.assertEqual(replace.call_count, 1)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native sharing semantics')
    def test_short_reader_does_not_fail_atomic_status_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            _atomic_json(path, {'status': 'running'})
            reader = path.open('rb')
            release = threading.Timer(0.05, reader.close)
            release.start()
            try:
                _atomic_json(path, {'status': 'complete'})
            finally:
                release.join()
                reader.close()
            self.assertEqual(json.loads(path.read_text())['status'], 'complete')
