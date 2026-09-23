import tempfile
import unittest
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle import research_schedule as schedule


class PreparationScheduleTests(unittest.TestCase):
    def test_prestart_worker_exits_without_blocking_price_maintenance(self):
        calls = []
        lab = SimpleNamespace(
            prepare_if_due=lambda **kw: {'allow_result_refresh': True},
            refresh_labels_if_due=lambda: calls.append('prices') or {'status': 'running'},
            wait_result_refresh=lambda *a, **kw: calls.append('wait'))
        with tempfile.TemporaryDirectory() as tmp:
            with patch('shaq_daily_oracle.lab_service.LabService', return_value=lab), \
                    patch.object(schedule, 'schedule_status', return_value={'enabled': True, 'start_et': '08:35'}), \
                    patch.object(schedule, 'due_status', return_value='waiting'):
                self.assertEqual(schedule.run_research_worker(SimpleNamespace(research_root=Path(tmp))), 0)
        self.assertEqual(calls, [], 'non-overlapping OS worker must exit before scheduled analysis')

    def test_preparation_window_uses_exchange_timezone_and_never_runs_weekend(self):
        self.assertTrue(hasattr(schedule,'preparation_window'))
        now=lambda day,h,m:datetime(2026,9,day,h,m,tzinfo=ZoneInfo('America/New_York'))
        self.assertIsNone(schedule.preparation_window(now(23,8,14),'08:35',20))
        self.assertEqual(schedule.preparation_window(now(23,8,15),'08:35',20),now(23,8,35))
        self.assertIsNone(schedule.preparation_window(now(23,8,35),'08:35',20))
        self.assertIsNone(schedule.preparation_window(now(26,8,20),'08:35',20))
        self.assertEqual(schedule.preparation_window(now(23,8,20).astimezone(ZoneInfo('Asia/Hong_Kong')),'08:35',20),now(23,8,35))

    def test_waiting_scheduler_prepares_without_starting_predictions(self):
        calls=[]
        lab=SimpleNamespace(prepare_if_due=lambda **kw:calls.append('prepare') or {},
                            refresh_labels_if_due=lambda:calls.append('prices') or {'status':'not_due'})
        with tempfile.TemporaryDirectory() as tmp:
            paths=SimpleNamespace(research_root=Path(tmp))
            with patch('shaq_daily_oracle.lab_service.LabService',return_value=lab), \
                    patch.object(schedule,'schedule_status',return_value={'enabled':True,'start_et':'08:35'}), \
                    patch.object(schedule,'due_status',return_value='waiting'):
                schedule.run_research_worker(paths)
        self.assertEqual(calls,['prepare'])

    def test_old_price_update_never_holds_prediction_scheduler_lock(self):
        from filelock import FileLock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def refresh():
                with FileLock(str(root/'schedule.lock'),timeout=0):
                    return {'status':'not_due'}
            lab=SimpleNamespace(refresh_labels_if_due=refresh)
            with patch('shaq_daily_oracle.lab_service.LabService',return_value=lab), \
                    patch.object(schedule,'schedule_status',return_value={'enabled':False,'start_et':'08:35'}):
                schedule.run_research_worker(SimpleNamespace(research_root=root))
            self.assertFalse((root/'schedule_status.json').exists(),
                             'settlement must not monopolize the scheduler or overwrite its status')

    def test_failed_warmup_does_not_poison_forecast_ledger(self):
        clock = {'now': datetime(2026, 9, 23, 8, 20, tzinfo=schedule.ET)}

        class TestClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock['now'].astimezone(tz) if tz else clock['now']

        calls = []

        class Lab:
            def prepare_if_due(self, **kwargs):
                calls.append('warmup')
                raise RuntimeError('history source unavailable')

            def refresh_labels_if_due(self):
                calls.append('prices')
                return {'status': 'not_due'}

            def start_batch(self, **kwargs):
                calls.append('forecast')
                return {'job_id': 'job-fixture', 'status': 'queued'}

            def job_statuses(self):
                return [{'job_id': 'job-fixture', 'status': 'complete'}]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = SimpleNamespace(research_root=root)
            with patch('shaq_daily_oracle.lab_service.LabService', return_value=Lab()), \
                    patch.object(schedule, 'schedule_status', return_value={
                        'enabled': True, 'start_et': '08:35', 'selections': [],
                        'model_profile_id': 'fixture'}), \
                    patch.object(schedule, 'datetime', TestClock):
                self.assertEqual(schedule.run_research_worker(paths), 0)
                self.assertFalse((root/'automatic_runs'/'2026-09-23.json').exists())
                self.assertEqual(calls, ['warmup'])
                clock['now'] = datetime(2026, 9, 23, 8, 35, tzinfo=schedule.ET)
                self.assertEqual(schedule.run_research_worker(paths), 0)
            self.assertEqual(json.loads((root/'automatic_runs'/'2026-09-23.json').read_text())['status'], 'complete')
        self.assertEqual(calls[:2], ['warmup', 'forecast'])

    def test_warmup_crossing_deadline_starts_forecast_in_same_worker(self):
        clock = {'now': datetime(2026, 9, 23, 8, 34, tzinfo=schedule.ET)}

        class TestClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock['now'].astimezone(tz) if tz else clock['now']

        calls = []

        class Lab:
            def prepare_if_due(self, **kwargs):
                calls.append('warmup')
                clock['now'] = datetime(2026, 9, 23, 8, 35, tzinfo=schedule.ET)
                return {'status': 'deadline_exceeded'}

            def refresh_labels_if_due(self):
                calls.append('prices')
                return {'status': 'not_due'}

            def start_batch(self, **kwargs):
                calls.append('forecast')
                return {'job_id': 'job-fixture', 'status': 'queued'}

            def job_statuses(self):
                return [{'job_id': 'job-fixture', 'status': 'complete'}]

        with tempfile.TemporaryDirectory() as tmp:
            paths = SimpleNamespace(research_root=Path(tmp))
            with patch('shaq_daily_oracle.lab_service.LabService', return_value=Lab()), \
                    patch.object(schedule, 'schedule_status', return_value={
                        'enabled': True, 'start_et': '08:35', 'selections': [],
                        'model_profile_id': 'fixture'}), \
                    patch.object(schedule, 'datetime', TestClock):
                self.assertEqual(schedule.run_research_worker(paths), 0)
        self.assertEqual(calls[:2], ['warmup', 'forecast'])
