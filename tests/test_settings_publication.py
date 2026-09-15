import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from shaq_daily_oracle.settings import _atomic_json


class SettingsPublicationTests(unittest.TestCase):
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
