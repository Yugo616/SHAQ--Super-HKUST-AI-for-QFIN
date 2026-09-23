import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from filelock import FileLock
from shaq_daily_oracle.lab_service import LabService


class BackgroundLifecycleTests(unittest.TestCase):
    def test_prediction_start_failure_is_terminal_and_can_be_retried(self):
        import test_research_lab_foundation as foundation
        import test_research_batch as fixtures
        from dataclasses import replace
        helper = fixtures.ResearchBatchTests()
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            profile = replace(helper.profile(), protocol='codex-cli', model='fixture-model')
            variant = helper.main_variant(lab.registry)
            for _ in range(2):
                with patch.object(lab, '_require_today_available'), \
                        patch.object(lab, '_resolve_variants', return_value=[variant]), \
                        patch.object(lab.settings, 'model_profile', return_value=profile), \
                        patch('shaq_daily_oracle.lab_service.start_guarded_thread', side_effect=RuntimeError('cannot start')):
                    with self.assertRaises(RuntimeError):
                        lab.start_batch(selections=[])
                job = next(iter(lab.jobs.values()))
                self.assertEqual(job['status'], 'failed')
                self.assertTrue(job['completed_at_et'])
                self.assertEqual(set(job['variant_progress'].values()), {'failed'})
                with FileLock(str(lab.paths.research_root/'jobs'/(job['job_id']+'.lock')), timeout=0):
                    pass

    def test_activity_detects_exit_without_any_receipt_write(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            lab=LabService.__new__(LabService)
            lab.paths=SimpleNamespace(research_root=Path(tmp))
            lab.jobs_lock=threading.Lock();lab.jobs={}
            lab.result_refresh_status=lambda:{'status':'idle'}
            lab.job_statuses=Mock(return_value=[{'job_id':'job-x','status':'running'}])
            first=lab.activity_status()
            lab.job_statuses.return_value=[{'job_id':'job-x','status':'incomplete','live_state':'interrupted'}]
            second=lab.activity_status(first['revision'])
            self.assertTrue(second['changed'])
            self.assertEqual(second['jobs'][0]['status'],'incomplete')

    def test_worker_start_failure_releases_lock_and_records_failure(self):
        from test_result_refresh import ResultRefreshTests
        with tempfile.TemporaryDirectory() as tmp:
            lab=ResultRefreshTests().service(Path(tmp))
            with patch('shaq_daily_oracle.lab_service.start_guarded_thread',side_effect=RuntimeError('cannot start thread')):
                with self.assertRaises(RuntimeError):
                    lab.start_result_refresh(manual=True)
            self.assertEqual(lab.result_refresh_status()['status'],'failed')
            with FileLock(str(Path(tmp)/'result_refresh.lock'),timeout=0):
                pass

    def test_read_state_never_starts_price_network_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService.__new__(LabService)
            lab.paths = SimpleNamespace(research_root=Path(tmp))
            lab.settings = Mock()
            lab.settings.public_settings.return_value = {}
            lab.registry = Mock()
            lab.registry.list_drafts.return_value = []
            lab.dashboard = Mock()
            lab.dashboard.overview.return_value = {}
            lab.history_linked_versions = lambda: []
            lab.data_status = lambda: {}
            lab.job_statuses = lambda: []
            lab.result_refresh_status = lambda: {}
            lab.start_result_refresh = Mock()
            lab.state()
            lab.start_result_refresh.assert_not_called()

    def test_crashed_price_refresh_is_not_displayed_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            lab=LabService.__new__(LabService)
            lab.paths=SimpleNamespace(research_root=root)
            receipt={'status':'running','operation_id':'crashed','attempted_at':'2026-09-22T17:00:00-04:00'}
            path=root/'label_refresh_status.json'
            path.write_text(json.dumps(receipt))
            lock=FileLock(str(root/'result_refresh.lock'))
            with lock:
                self.assertEqual(lab.result_refresh_status()['status'],'running')
            result=lab.result_refresh_status()
            self.assertEqual(result['status'],'interrupted')
            self.assertEqual(json.loads(path.read_text()),receipt,'display must not rewrite saved outcome')

    def test_existing_job_lock_is_authority_not_stale_running_json(self):
        from shaq_daily_oracle.job_corrections import observed_job_status
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'jobs').mkdir()
            job={'job_id':'job-x','status':'running','variant_progress':{'a':'complete','b':'running'}}
            lock=FileLock(str(root/'jobs/job-x.lock'))
            with lock:
                self.assertEqual(observed_job_status(root,job)['status'],'running')
            row=observed_job_status(root,job)
            self.assertEqual(row['status'],'incomplete')
            self.assertEqual(row['variant_progress'],{'a':'complete','b':'incomplete'})
            self.assertEqual(job['status'],'running')

    def test_price_refresh_failure_does_not_fail_finished_research(self):
        from test_collection_failure_state import CollectionFailureStateTests
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);lab=CollectionFailureStateTests().service(root)
            lab.paths.package_root=Path(__file__).resolve().parents[1]
            lab.paths.batches_root=root/'batches'
            lab.registry=SimpleNamespace()
            lab.settings.execution_policy=lambda:None
            with patch.object(lab,'_today_evidence',return_value=object()), \
                    patch('shaq_daily_oracle.lab_service.ResearchBatchRunner') as runner, \
                    patch.object(lab,'start_result_refresh',side_effect=RuntimeError('network unavailable')):
                runner.return_value.run.return_value={'status':{'all_variants_completed':True,
                    'completed_variants':['team/a'],'failed_variants':{},'batch_id':'LAB-fixture'}}
                lab._run_batch_job(job_id='job-fixture',variants=[],profile=None,secret='')
            row=json.loads((root/'jobs/job-fixture.json').read_text())
            self.assertEqual(row['status'],'complete')
            self.assertEqual(row['label_refresh']['status'],'failed')
