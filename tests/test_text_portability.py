"""Exercise UTF-8 resource contracts with Windows' legacy text fallback."""
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import json
import tempfile
import unittest
from unittest.mock import patch


@contextmanager
def legacy_text_locale():
    original = Path.open
    def open_file(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if 'b' not in mode and encoding in (None, 'locale'):
            encoding = 'cp1252'
        return original(path, mode, buffering, encoding, errors, newline)
    with patch.object(Path, 'open', open_file), patch('subprocess._text_encoding', return_value='cp1252'):
        yield


class TextPortabilityTests(unittest.TestCase):
    def test_chinese_account_view_round_trips_under_legacy_locale(self):
        from test_account_view import AccountViewTests
        with legacy_text_locale():
            html = AccountViewTests().render('dayHtml', {'status': 'pending', 'orders': [], 'trades': []})
        self.assertIn('等待', html)

    def test_saved_chinese_legacy_record_reopens_without_changing_hash(self):
        from shaq_daily_oracle.hashing import sha256_payload
        from shaq_daily_oracle.virtual_accounts import AccountStore
        document = {'status': 'saved', 'note': '中文研究紀錄，不重新計算'}
        digest = sha256_payload(document)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / 'settlements' / (digest + '.json')
            path.parent.mkdir()
            path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
            with legacy_text_locale():
                result = AccountStore(root).read_legacy()
            self.assertEqual(result['results'][0]['note'], document['note'])
            self.assertEqual(result['results'][0]['settlement_hash'], digest)

    def test_windows_audit_paths_use_portable_slashes(self):
        from test_native_packaging import NativePackagingTests
        audit = NativePackagingTests().module('audit_payload')
        class WindowsPayload(PureWindowsPath):
            def rglob(self, pattern):
                return [self / 'runtime', self / 'runtime/settings.json']
        self.assertEqual(audit.forbidden_paths(WindowsPayload('C:/payload')),
                         ['runtime', 'runtime/settings.json'])
