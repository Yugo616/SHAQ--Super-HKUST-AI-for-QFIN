import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
from shaq_daily_oracle.public_data import DailyBarCache
from shaq_daily_oracle.public_history_recovery import PublicHistoryRecovery

ET = ZoneInfo('America/New_York')


class PreparedHistoryReuseTests(unittest.TestCase):
    def exercise(self, *, missing_day=False, missing_receipt=False, captured='2026-09-24T07:45:00-04:00',
                 latest_close=101, corrupt_cache=False):
        # A real verified disk cache and receipt, with only the network boundary replaced.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            provider = PublicHistoryRecovery(YFinanceProvider(DataProfile('test', 'unused')), {},
                                             checkpoint_root=root/'checkpoints')
            raw = '{"source":"test historical response"}'
            digest = hashlib.sha256(raw.encode()).hexdigest()
            receipts = root/'checkpoints'/'sources'
            receipts.mkdir(parents=True)
            if not missing_receipt:
                (receipts/(digest+'.json')).write_text(json.dumps(
                    {'raw': raw, 'response_sha256': digest, 'captured_at': captured}))
            rows = [{'timestamp': f'2026-09-{day}T00:00:00', 'open': 100, 'high': 102,
                     'low': 99, 'close': latest_close if day == 23 else 101, 'volume': 10,
                     'source_response_sha256': digest, 'captured_at': captured}
                    for day in (21, 22, 23) if not (missing_day and day == 22)]
            cache = DailyBarCache(provider, root/'bars', overlap_days=3)
            cache._save('AAA', {}, rows, start=date(2026,9,21), end=date(2026,9,24))
            if corrupt_cache:
                path = next((root/'bars').glob('*.json'))
                content = json.loads(path.read_text()); content['sha256'] = 'invalid'
                path.write_text(json.dumps(content))
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime(2026,9,24,8,35,tzinfo=ET)
            with patch('shaq_daily_oracle.public_history_recovery.datetime', Clock), \
                 patch.object(provider, 'history', return_value={'AAA': rows}) as network:
                result = cache.history(['AAA'], start=date(2026,9,21), end=date(2026,9,24))
            return result, network.call_count, provider.diagnostics, provider.source_documents

    def test_collection_reuses_same_morning_complete_preparation_without_network(self):
        result, calls, diagnostics, sources = self.exercise()
        self.assertEqual(calls, 0)
        self.assertEqual(len(result['AAA']), 3)
        self.assertEqual(diagnostics[-1]['status'], 'reused')
        self.assertEqual(len(sources), 1)

    def test_missing_interior_day_or_close_must_refetch(self):
        for options in ({'missing_day': True}, {'latest_close': None}):
            with self.subTest(options=options):
                self.assertEqual(self.exercise(**options)[1], 1)

    def test_yesterday_or_future_or_missing_capture_must_refetch(self):
        for stamp in ('2026-09-23T17:00:00-04:00', '2026-09-24T09:00:00-04:00', ''):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.exercise(captured=stamp)[1], 1)

    def test_corrupt_cache_must_refetch(self):
        self.assertEqual(self.exercise(corrupt_cache=True)[1], 1)

    def test_missing_receipt_never_counts_as_prepared(self):
        # Failed source validation must not silently approve the saved data.
        from shaq_daily_oracle.data_providers import DataProviderError
        with self.assertRaises(DataProviderError):
            self.exercise(missing_receipt=True)
