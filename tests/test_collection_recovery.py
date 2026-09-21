"""Network faults must not erase completed acquisition or disable all recovery."""
import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import yfinance as yf
from curl_cffi.curl import CurlError

from shaq_daily_oracle import research_schedule as schedule
from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider
from shaq_daily_oracle.data_retry import sanitize_diagnostic, failure_message


class CollectionRecoveryTests(unittest.TestCase):
    def test_worker_deadline_preserves_request_scope_without_guessing_failed_symbol(self):
        from shaq_daily_oracle.collection_worker import call_in_worker
        payload = {'symbols': ['AAA', 'BBB', 'https://private/key'],
                   'start': '2026-09-01', 'end': '2026-09-21', 'interval': '1d'}
        with patch('shaq_daily_oracle.collection_worker.run_model_process',
                   side_effect=subprocess.TimeoutExpired('worker', 1)):
            with self.assertRaises(DataProviderError) as caught:
                call_in_worker('history', DataProfile('test', 'unused'), payload)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic.get('requested_symbols'), ['AAA', 'BBB'])
        self.assertEqual(diagnostic.get('request_start'), '2026-09-01')
        self.assertEqual(diagnostic.get('request_end'), '2026-09-21')
        self.assertEqual(diagnostic.get('interval'), '1d')
        self.assertNotIn('symbol', diagnostic)
        self.assertNotIn('private', json.dumps(diagnostic))

    def test_partial_daily_history_survives_failure_and_restart(self):
        # Removing the per-ticker checkpoint would request AAA and CCC twice.
        calls = []
        failing = True
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            if ticker.ticker == 'BBB' and failing:
                raise CurlError('private response', code=28)
            return pd.DataFrame({'Open': [123], 'Close': [124], 'Volume': [7]},
                                index=pd.DatetimeIndex(['2026-09-18'], tz='America/New_York'))
        with tempfile.TemporaryDirectory() as directory, patch.object(yf.Ticker, 'history', history):
            profile = DataProfile('test', 'unused', yahoo_request_max_retries=0)
            def provider():
                item = YFinanceProvider(profile)
                item.history_checkpoint_root = Path(directory)
                return item
            with self.assertRaises(DataProviderError) as failed:
                provider()._history_inline(['AAA', 'BBB', 'CCC'], start=date(2026, 9, 1),
                                          end=date(2026, 9, 21), session=None)
            self.assertEqual(calls, ['AAA', 'BBB', 'CCC'])
            self.assertEqual(failed.exception.diagnostic.get('symbol'), 'BBB')
            self.assertEqual(failed.exception.diagnostic.get('completed_symbols'), 2)
            self.assertEqual(failed.exception.diagnostic.get('interval'), '1d')
            failing = False
            rows = provider()._history_inline(['AAA', 'BBB', 'CCC'], start=date(2026, 9, 1),
                                            end=date(2026, 9, 21), session=None)
            self.assertEqual(calls, ['AAA', 'BBB', 'CCC', 'BBB'])
            self.assertEqual(rows['AAA'][0]['open'], 123)
            self.assertEqual(rows['BBB'][0]['timestamp'], '2026-09-18T00:00:00')

    def test_checkpoints_do_not_replace_fresh_intraday_or_different_date_requests(self):
        calls = []
        def history(ticker, **kwargs):
            calls.append(kwargs['interval'])
            return pd.DataFrame({'Open': [123]},
                                index=pd.DatetimeIndex(['2026-09-18T08:30'], tz='America/New_York'))
        with tempfile.TemporaryDirectory() as directory, patch.object(yf.Ticker, 'history', history):
            provider = YFinanceProvider(DataProfile('test', 'unused'))
            provider.history_checkpoint_root = Path(directory)
            for interval, end in [('1d', 21), ('1d', 21), ('1d', 22), ('5m', 21), ('5m', 21)]:
                provider._history_inline(['AAA'], start=date(2026, 9, 1),
                                         end=date(2026, 9, end), interval=interval, session=None)
            self.assertEqual(calls, ['1d', '1d', '5m', '5m'])

    def test_diagnostic_retains_only_safe_request_identity(self):
        value = sanitize_diagnostic({'kind': 'timeout', 'symbol': 'BRK-B', 'interval': '1d',
                                     'completed_symbols': 499, 'failed_symbols': 1,
                                     'request_start': '2026-08-01', 'request_end': '2026-09-21',
                                     'headers': {'Authorization': 'private'}})
        self.assertEqual(value.get('symbol'), 'BRK-B')
        self.assertEqual(value.get('completed_symbols'), 499)
        self.assertNotIn('private', json.dumps(value))
        self.assertIn('BRK-B', failure_message(value))
        self.assertNotIn('symbol', sanitize_diagnostic({'symbol': 'https://secret/key'}))

    def test_request_and_worker_timeout_messages_are_distinct(self):
        self.assertNotIn('总时限', failure_message({'kind': 'timeout', 'curl_code': 28}))
        self.assertIn('总时限', failure_message({'kind': 'timeout', 'timeout_scope': 'worker'}))

    def test_corrupt_checkpoint_is_refetched_and_empty_result_is_not_pinned(self):
        calls = []
        empty = False
        def history(ticker, **kwargs):
            calls.append(ticker.ticker)
            return pd.DataFrame() if empty else pd.DataFrame({'Open': [123]},
                index=pd.DatetimeIndex(['2026-09-18'], tz='UTC'))
        with tempfile.TemporaryDirectory() as directory, patch.object(yf.Ticker, 'history', history):
            provider = YFinanceProvider(DataProfile('test', 'unused'))
            provider.history_checkpoint_root = Path(directory)
            def read():
                return provider._history_inline(['AAA'], start=date(2026, 9, 1),
                    end=date(2026, 9, 21), session=None)
            read()
            checkpoint = next(Path(directory).glob('*.json'))
            content = json.loads(checkpoint.read_text())
            content['rows'][0]['open'] = 999
            checkpoint.write_text(json.dumps(content))
            self.assertEqual(read()['AAA'][0]['open'], 123)
            checkpoint.unlink()
            empty = True
            self.assertEqual(read(), {'AAA': []})
            self.assertFalse(list(Path(directory).glob('*.json')))
            empty = False
            self.assertEqual(read()['AAA'][0]['open'], 123)
            self.assertEqual(len(calls), 4)

    def run_worker(self, root, saved, now, terminal, overrides=None):
        ledger = root / 'automatic_runs' / (now.date().isoformat() + '.json')
        ledger.parent.mkdir(exist_ok=True)
        if saved is not None:
            ledger.write_text(json.dumps(saved))
        calls = []
        class Lab:
            def start_batch(self, **kwargs):
                calls.append(kwargs)
                return {'job_id': 'job-fixture', 'status': 'queued'}
            def job_statuses(self):
                return [terminal]
            def refresh_labels_if_due(self):
                calls.append('price_refresh')
                return {'status': 'not_due'}
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        value = {'enabled': True, 'start_et': '08:35', 'selections': [{'version_id': 'main'}],
                 'model_profile_id': 'fixture', **(overrides or {})}
        with patch('shaq_daily_oracle.lab_service.LabService', return_value=Lab()), \
             patch.object(schedule, 'schedule_status', return_value=value), \
             patch.object(schedule, 'datetime', Clock):
            self.assertEqual(schedule.run_research_worker(SimpleNamespace(research_root=root)), 0)
        self.last_price_refresh_count = calls.count('price_refresh')
        return [call for call in calls if call != 'price_refresh'], json.loads(ledger.read_text())

    def test_pending_collection_retry_does_not_wait_for_historical_settlement_refresh(self):
        now = datetime(2026, 9, 21, 8, 42, tzinfo=schedule.ET)
        with tempfile.TemporaryDirectory() as directory:
            self.run_worker(Path(directory), None, now, self.failure(now))
            self.assertEqual(self.last_price_refresh_count, 0)

    def test_worker_carries_checkpoint_to_real_child_and_recovery_uses_it(self):
        from dataclasses import replace
        from shaq_daily_oracle import collection_worker
        from shaq_daily_oracle.model_execution import run_model_process
        import sys
        # The provider/worker/checkpoint are real; only the network response is replaced.
        script = '''
import pandas as pd
import yfinance as yf
from unittest.mock import patch
from curl_cffi.curl import CurlError
from shaq_daily_oracle.collection_worker import main
def history(ticker, **kwargs):
    if ticker.ticker == 'BBB': raise CurlError('private', code=28)
    return pd.DataFrame({'Open':[123]}, index=pd.DatetimeIndex(['2026-09-18'],tz='UTC'))
with patch.object(yf.Ticker, 'history', history): main()
'''
        with tempfile.TemporaryDirectory() as directory:
            provider = YFinanceProvider(DataProfile('test', 'unused', yahoo_request_max_retries=0))
            provider.history_checkpoint_root = Path(directory)
            def run(command, **kwargs):
                return run_model_process([sys.executable, '-c', script], **kwargs)
            with patch.object(collection_worker, 'run_model_process', side_effect=run):
                with self.assertRaises(DataProviderError) as caught:
                    provider.history(['AAA', 'BBB'], start=date(2026, 9, 1), end=date(2026, 9, 21))
            self.assertEqual(caught.exception.diagnostic['symbol'], 'BBB')
            self.assertEqual(len(list(Path(directory).glob('*.json'))), 1)
            provider = YFinanceProvider(replace(provider.profile, maximum_candidates=3,
                                               request_timeout_seconds=19))
            provider.history_checkpoint_root = Path(directory)
            self.assertEqual(provider.recover_history(['AAA'], start=date(2026, 9, 1),
                              end=date(2026, 9, 21))['AAA'][0]['open'], 123)
            script = script.replace("if ticker.ticker == 'BBB': raise CurlError('private', code=28)",
                                    "if ticker.ticker == 'AAA': raise AssertionError('must reuse AAA')")
            with patch.object(collection_worker, 'run_model_process', side_effect=run):
                rows = provider.history(['AAA', 'BBB'], start=date(2026, 9, 1), end=date(2026, 9, 21))
            self.assertEqual(rows['AAA'][0]['open'], 123)
            self.assertEqual(rows['BBB'][0]['open'], 123)

    def test_retry_waits_for_backoff_and_saved_count_survives_worker_restart(self):
        now = datetime(2026, 9, 21, 8, 42, tzinfo=schedule.ET)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed = {**self.failure(now), 'completed_at_et': now.isoformat()}
            calls, saved = self.run_worker(root, failed, now, failed)
            self.assertEqual(calls, [])
            calls, saved = self.run_worker(root, saved, now + timedelta(seconds=31), failed)
            self.assertEqual(len(calls), 1)
            calls, saved = self.run_worker(root, saved, now + timedelta(seconds=90), failed)
            self.assertEqual(calls, [])

    def failure(self, now):
        return {'job_id': 'job-fixture', 'status': 'failed', 'error_type': 'ResearchCollectionError',
                'completed_at_et': (now - timedelta(minutes=2)).isoformat(),
                'error_diagnostic': {'kind': 'timeout', 'retryable': True, 'stage': 'history'}}

    def test_scheduler_recovers_transient_collection_only_once_and_keeps_attempt(self):
        now = datetime(2026, 9, 21, 8, 42, tzinfo=schedule.ET)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.failure(now)
            calls, saved = self.run_worker(root, original, now, self.failure(now))
            self.assertEqual(len(calls), 1, 'transient collection failure must not be permanently terminal')
            self.assertEqual(saved.get('collection_recovery_count'), 1)
            receipts = list((root / 'automatic_runs' / 'attempts').glob('*.json'))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text()), original)
            calls, _ = self.run_worker(root, saved, now + timedelta(minutes=2), self.failure(now))
            self.assertEqual(calls, [], 'automatic recovery must have a finite budget')

    def test_scheduler_does_not_retry_models_auth_complete_or_after_cutoff(self):
        now = datetime(2026, 9, 21, 8, 42, tzinfo=schedule.ET)
        failure = self.failure(now)
        cases = [(failure, now.replace(hour=8, minute=50)),
                 ({**failure, 'error_type': 'ModelBackendError'}, now),
                 ({**failure, 'batch_id': 'LAB-frozen'}, now),
                 ({**failure, 'error_diagnostic': {'kind': 'auth_error'}}, now),
                 ({**failure, 'status': 'complete'}, now),
                 ({**failure, 'status': 'partial_failure'}, now)]
        for saved, clock in cases:
            with self.subTest(saved=saved, clock=clock), tempfile.TemporaryDirectory() as directory:
                calls, after = self.run_worker(Path(directory), saved, clock, failure)
                self.assertEqual(calls, [])
                self.assertEqual(after, saved)
