from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class FileRevisionTests(unittest.TestCase):
    def test_windows_creation_time_cannot_hide_same_size_rewrite(self):
        from shaq_daily_oracle import hashing
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'evidence.json'
            path.write_bytes(b'{"price":100}')
            stat = path.stat()
            fixed = SimpleNamespace(**{key: getattr(stat, key) for key in
                ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_ino', 'st_dev')})
            with patch.object(hashing.sys, 'platform', 'win32'), patch.object(type(path), 'stat', return_value=fixed):
                before = hashing.file_revision(path)
                path.write_bytes(b'{"price":200}')
                self.assertNotEqual(before, hashing.file_revision(path))

    def test_posix_revision_does_not_rehash_unchanged_bytes(self):
        from shaq_daily_oracle import hashing
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'evidence.json'
            path.write_bytes(b'{}')
            with patch.object(hashing.sys, 'platform', 'darwin'), \
                    patch.object(hashing, 'sha256_file', side_effect=AssertionError('unneeded hash')):
                self.assertEqual(hashing.file_revision(path), hashing.file_revision(path))
