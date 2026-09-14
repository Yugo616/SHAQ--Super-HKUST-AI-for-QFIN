import errno
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from filelock import FileLock
from shaq_daily_oracle.lab_service import LabService
from shaq_daily_oracle.hashing import sha256_file


class CollectionFailureStateTests(unittest.TestCase):
    def service(self, root):
        service = LabService.__new__(LabService)
        service.paths = SimpleNamespace(research_root=root)
        service.jobs = {'job-fixture': {'job_id':'job-fixture','status':'queued'}}
        service.jobs_lock = threading.Lock()
        service.settings = SimpleNamespace(load=lambda:{
            'data_profile':{'profile_id':'fixture','universe_file':'unused'}, 'sec_identity':'private'})
        return service

    def test_child_collection_error_is_persisted_and_owned_job_lock_released(self):
        from shaq_daily_oracle.data_providers import DataProviderError
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = self.service(root)
            lock = FileLock(str(root/'job-fixture.lock'))
            lock.acquire()
            with patch.object(service, '_today_evidence', side_effect=DataProviderError('Yahoo collection worker crashed')):
                service._run_batch_job(job_id='job-fixture', variants=[], profile=None, secret='', task_lock=lock)
            saved = json.loads((root/'jobs/job-fixture.json').read_text())
            self.assertEqual(saved['status'], 'failed')
            self.assertIsNotNone(saved['completed_at_et'])
            self.assertFalse(lock.is_locked)
            self.assertEqual(service.job_statuses()[0]['status'], 'failed')

    def test_initial_persistence_failure_still_terminates_in_memory_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = self.service(root)
            lock = FileLock(str(root/'job-fixture.lock')); lock.acquire()
            with patch('shaq_daily_oracle.lab_service._atomic_json', side_effect=OSError(errno.EMFILE,'too many files')):
                service._run_batch_job(job_id='job-fixture', variants=[], profile=None, secret='', task_lock=lock)
            self.assertFalse(lock.is_locked)
            status = service.job_statuses()[0]
            self.assertEqual(status['status'], 'failed')
            self.assertTrue(status['persistence_error'])

    @unittest.skipIf(sys.platform == 'win32', 'POSIX EMFILE fixture')
    def test_real_child_emfile_crash_and_timeout_leave_parent_failure_reason_writable(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
        from shaq_daily_oracle.research_collection import collect_research_evidence
        from shaq_daily_oracle import collection_worker
        from shaq_daily_oracle.model_execution import run_model_process
        emfile = r'''
import os, resource
import yfinance as yf
from unittest.mock import patch
from shaq_daily_oracle import collection_worker
def exhaust(ticker, **kwargs):
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(64,hard),hard))
    handles=[]
    try:
        while True: handles.append(open(os.devnull))
    finally:
        for handle in handles: handle.close()
with patch.object(yf.Ticker, 'history', exhaust):
    raise SystemExit(collection_worker.main())
'''
        for script, kind, timeout in [(emfile,'resource_exhausted',5),
                                      ('import os;os._exit(9)','worker_crash',5),
                                      ('import time;time.sleep(10)','timeout',.3)]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                root=Path(name); service=self.service(root)
                def read(**kwargs):
                    profile=DataProfile('test','config/research-universe.csv',yahoo_worker_timeout_seconds=timeout)
                    return collect_research_evidence(root=root/'evidence',
                        package_root=Path(__file__).resolve().parents[1], profile=profile,
                        sec_identity='Fixture fixture@example.invalid',
                        observed_at=datetime(2026,9,14,8,40,tzinfo=ZoneInfo('America/New_York')),
                        market_provider=YFinanceProvider(profile),
                        metadata_provider=SimpleNamespace(),event_provider=SimpleNamespace())
                def fixture_child(command, **kwargs):
                    return run_model_process([sys.executable,'-c',script],**kwargs)
                lock=FileLock(str(root/'job-fixture.lock'));lock.acquire()
                with patch.object(service,'_today_evidence',side_effect=read), \
                     patch.object(collection_worker,'run_model_process',side_effect=fixture_child):
                    service._run_batch_job(job_id='job-fixture',variants=[],profile=None,secret='',task_lock=lock)
                saved=json.loads((root/'jobs/job-fixture.json').read_text())
                self.assertEqual(saved['status'],'failed')
                self.assertEqual(saved['error_diagnostic']['kind'],kind)
                self.assertFalse(lock.is_locked)
                if kind=='resource_exhausted':
                    self.assertEqual(saved['error_diagnostic']['errno'],24)
                    self.assertEqual(saved['error_diagnostic']['error_type'],'OSError')

    def correction_fixture(self, root):
        job = {'job_id':'job-fixture','status':'running',
               'started_at_et':'2026-09-14T08:39:23.440295-04:00',
               'completed_at_et':None, 'variant_progress':{'team/main':'queued'}}
        failed = {**job,'status':'failed','completed_at_et':'2026-09-14T08:39:33.058359-04:00',
                  'error_type':'OSError','message':'[Errno 24] Too many open files: fixture.tmp'}
        paths = [root/'jobs/job-fixture.json', root/'automatic_runs/2026-09-14.json']
        for path, value in zip(paths, [job, failed]):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding='utf-8')
        return paths, {'job_id':'job-fixture','trade_date':'2026-09-14',
                       'job_sha256':sha256_file(paths[0]),'automatic_run_sha256':sha256_file(paths[1])}

    def test_correction_is_append_only_hash_bound_and_shown_after_restart(self):
        self.assertTrue(hasattr(LabService, 'correct_interrupted_job'), 'append-only recovery is missing')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); service = self.service(root); service.jobs = {}
            paths, request = self.correction_fixture(root)
            frozen = root/'evidence/original/evidence_manifest.json'
            frozen.parent.mkdir(parents=True); frozen.write_bytes(b'original frozen evidence')
            originals = {path:path.read_bytes() for path in [*paths, frozen]}
            receipt = service.correct_interrupted_job(**request)
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(service.job_statuses()[0]['status'], 'failed')
            self.assertTrue(service.job_statuses()[0]['status_correction'])
            restarted = self.service(root); restarted.jobs = {}
            self.assertEqual(restarted.job_statuses()[0]['status'], 'failed')
            self.assertEqual(restarted.correct_interrupted_job(**request), receipt)
            self.assertEqual(len(list((root/'job_corrections').glob('*.json'))), 1)
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)
            # A changed proof invalidates the overlay instead of laundering a new run.
            paths[1].write_text('{}')
            self.assertEqual(restarted.job_statuses()[0]['status'], 'running')

    def test_correction_refuses_active_locks_wrong_hashes_and_mismatched_failure(self):
        self.assertTrue(hasattr(LabService, 'correct_interrupted_job'))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); service = self.service(root); service.jobs = {}
            paths, request = self.correction_fixture(root)
            for lock_path in [root/'jobs/job-fixture.lock', root/'schedule.lock']:
                with FileLock(str(lock_path)):
                    with self.assertRaises(ValueError):
                        service.correct_interrupted_job(**request)
            with self.assertRaises(ValueError):
                service.correct_interrupted_job(**{**request,'job_sha256':'0'*64})
            failed = json.loads(paths[1].read_text()); failed['job_id'] = 'job-other'
            paths[1].write_text(json.dumps(failed))
            with self.assertRaises(ValueError):
                service.correct_interrupted_job(**{**request,'automatic_run_sha256':sha256_file(paths[1])})
            self.assertFalse((root/'job_corrections').exists())


if __name__ == '__main__':
    unittest.main()
