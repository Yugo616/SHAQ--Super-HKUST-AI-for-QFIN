import json
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import yfinance as yf

from shaq_daily_oracle import collection_worker
from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider


class YahooRetryProgressTests(unittest.TestCase):
    def test_transient_retry_events_mark_real_schedule_and_start_without_private_error(self):
        observed = []
        attempts = []
        provider = YFinanceProvider(
            DataProfile('fixture', 'unused', yahoo_retry_backoff_seconds=0.02),
            progress_observer=lambda **event: observed.append((event, datetime.now(timezone.utc))),
        )

        def request():
            attempts.append(True)
            if len(attempts) == 1:
                raise TimeoutError('Authorization: private value')
            return 'ready'

        self.assertEqual(provider._request_with_retry(request, stage='history', symbol='AAA'), 'ready')
        self.assertEqual(len(attempts), 2)
        self.assertEqual([row['stage'] for row, _ in observed], [
            'data_request_failed', 'data_retry_scheduled', 'data_retry_started',
        ])
        scheduled, started = observed[1], observed[2]
        self.assertEqual(scheduled[0]['attempt'], 2)
        self.assertEqual(scheduled[0]['max_attempts'], 2)
        self.assertEqual(scheduled[0]['source'], 'yfinance')
        self.assertEqual(scheduled[0]['request_stage'], 'history')
        self.assertEqual(scheduled[0]['symbol'], 'AAA')
        self.assertLessEqual(datetime.fromisoformat(scheduled[0]['next_retry_at']), started[1])
        self.assertNotIn('private', json.dumps([row for row, _ in observed]))

    def test_auth_failure_is_not_scheduled_for_retry(self):
        events = []
        provider = YFinanceProvider(DataProfile('fixture', 'unused'),
                                    progress_observer=lambda **event: events.append(event))
        denied = RuntimeError('Authorization: private value')
        denied.response = SimpleNamespace(status_code=403)
        with self.assertRaises(DataProviderError):
            provider._request_with_retry(lambda: (_ for _ in ()).throw(denied),
                                         stage='history', symbol='AAA')
        self.assertEqual([row['stage'] for row in events], ['data_request_failed'])
        self.assertEqual(events[0]['failure_kind'], 'auth_error')
        self.assertNotIn('private', json.dumps(events))

    def test_genuine_no_data_does_not_appear_as_failed_request(self):
        from yfinance.exceptions import YFPricesMissingError
        events = []
        provider = YFinanceProvider(DataProfile('fixture', 'unused'),
                                    progress_observer=lambda **event: events.append(event))
        with self.assertRaises(YFPricesMissingError):
            provider._request_with_retry(
                lambda: (_ for _ in ()).throw(YFPricesMissingError('AAA', 'private')),
                stage='history', symbol='AAA')
        self.assertEqual(events, [])

    def test_child_progress_file_records_actual_ticker_retry(self):
        attempts = []

        def history(ticker, **kwargs):
            attempts.append(ticker.ticker)
            if len(attempts) == 1:
                raise TimeoutError('private request text')
            return pd.DataFrame({'Open': [101]}, index=pd.DatetimeIndex(['2026-09-01'], tz='UTC'))

        with tempfile.TemporaryDirectory() as tmp, patch.object(yf.Ticker, 'history', history):
            path = Path(tmp) / 'progress.jsonl'
            result = collection_worker.execute_operation('history', {
                'profile': {'profile_id': 'fixture', 'universe_file': 'unused',
                            'yahoo_retry_backoff_seconds': 0},
                'symbols': ['AAA'], 'start': '2026-09-01', 'end': '2026-09-02',
                'cache_parent': tmp, 'progress_path': str(path),
            })
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(attempts, ['AAA', 'AAA'])
        self.assertEqual(result['AAA'][0]['open'], 101)
        self.assertEqual([row['stage'] for row in events], [
            'data_request_failed', 'data_retry_scheduled', 'data_retry_started',
        ])
        self.assertTrue(all(row['symbol'] == 'AAA' for row in events))

    def test_parent_forwards_progress_while_worker_is_still_running_and_cleans_scratch(self):
        received = threading.Event()
        scratch = []
        events = []

        def observer(**event):
            events.append(event)
            received.set()

        def child(command, **kwargs):
            payload = json.loads(kwargs['input'])['payload']
            path = Path(payload['progress_path'])
            scratch.append(path.parent)
            path.write_text(json.dumps({'stage': 'data_retry_scheduled', 'source': 'yfinance',
                'request_stage': 'history', 'symbol': 'AAA', 'attempt': 2,
                'max_attempts': 2, 'next_retry_at': '2026-09-23T12:00:00+00:00'}) + '\n')
            self.assertTrue(received.wait(1), 'progress must arrive before worker completion')
            return subprocess.CompletedProcess(command, 0, '{"result":{"AAA":[]}}', '')

        provider = YFinanceProvider(DataProfile('fixture', 'unused'), progress_observer=observer)
        with patch.object(collection_worker, 'run_model_process', side_effect=child):
            self.assertEqual(provider.history(['AAA'], start=date(2026, 9, 1),
                                              end=date(2026, 9, 2)), {'AAA': []})
        self.assertEqual(events[0]['symbol'], 'AAA')
        self.assertFalse(scratch[0].exists())

    def test_no_observer_does_not_create_progress_channel(self):
        def child(command, **kwargs):
            payload = json.loads(kwargs['input'])['payload']
            self.assertNotIn('progress_path', payload)
            self.assertEqual(list(Path(payload['cache_parent']).iterdir()), [])
            return subprocess.CompletedProcess(command, 0, '{"result":{"AAA":[]}}', '')

        with patch.object(collection_worker, 'run_model_process', side_effect=child):
            self.assertEqual(YFinanceProvider(DataProfile('fixture', 'unused')).history(
                ['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 2)), {'AAA': []})

    def test_unavailable_display_channel_does_not_cancel_data_request(self):
        def child(command, **kwargs):
            payload = json.loads(kwargs['input'])['payload']
            self.assertNotIn('progress_path', payload)
            return subprocess.CompletedProcess(command, 0, '{"result":{"AAA":[]}}', '')

        provider = YFinanceProvider(DataProfile('fixture', 'unused'),
                                    progress_observer=lambda **event: None)
        with patch.object(collection_worker.threading.Thread, 'start', side_effect=RuntimeError('display unavailable')), \
                patch.object(collection_worker, 'run_model_process', side_effect=child):
            self.assertEqual(provider.history(['AAA'], start=date(2026, 9, 1),
                                              end=date(2026, 9, 2)), {'AAA': []})

    def test_worker_timeout_drains_progress_and_reclaims_reader(self):
        received = []
        scratch = []
        before = {thread.ident for thread in threading.enumerate()
                  if thread.name == 'shaq-collection-progress'}

        def child(command, **kwargs):
            payload = json.loads(kwargs['input'])['payload']
            path = Path(payload['progress_path'])
            scratch.append(path.parent)
            path.write_text(json.dumps({'stage': 'data_retry_scheduled', 'source': 'yfinance',
                'request_stage': 'history', 'symbol': 'AAA', 'attempt': 2,
                'max_attempts': 2, 'next_retry_at': '2026-09-23T12:00:00+00:00'}) + '\n')
            raise subprocess.TimeoutExpired(command, 1)

        provider = YFinanceProvider(DataProfile('fixture', 'unused'),
                                    progress_observer=lambda **event: received.append(event))
        with patch.object(collection_worker, 'run_model_process', side_effect=child):
            with self.assertRaises(DataProviderError):
                provider.history(['AAA'], start=date(2026, 9, 1), end=date(2026, 9, 2))
        self.assertEqual([row['stage'] for row in received], ['data_retry_scheduled'])
        self.assertFalse(scratch[0].exists())
        self.assertEqual({thread.ident for thread in threading.enumerate()
                          if thread.name == 'shaq-collection-progress'}, before)


if __name__ == '__main__':
    unittest.main()
