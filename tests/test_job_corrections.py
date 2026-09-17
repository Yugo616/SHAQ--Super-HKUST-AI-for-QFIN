import json
from pathlib import Path
import tempfile
import unittest

from filelock import FileLock
from shaq_daily_oracle.hashing import sha256_file
from shaq_daily_oracle.job_corrections import append_correction, corrected_status


class CorrectionDisplayTests(unittest.TestCase):
    def test_another_daily_run_cannot_resurrect_a_verified_failed_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {'job_id': 'job-fixture', 'status': 'running',
                   'started_at_et': '2026-09-14T08:35:00-04:00'}
            failure = {**job, 'status': 'failed', 'error_type': 'OSError',
                       'message': 'resource exhaustion',
                       'completed_at_et': '2026-09-14T08:36:00-04:00'}
            job_path = root/'jobs/job-fixture.json'
            run_path = root/'automatic_runs/2026-09-14.json'
            for path, payload in ((job_path, job), (run_path, failure)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload))
            hashes = [sha256_file(job_path), sha256_file(run_path)]
            append_correction(root, job_id=job['job_id'], trade_date='2026-09-14',
                              job_sha256=hashes[0], automatic_run_sha256=hashes[1])
            with FileLock(str(root/'schedule.lock'), timeout=0):
                self.assertEqual(corrected_status(root, job)['status'], 'failed')
            self.assertEqual([sha256_file(job_path), sha256_file(run_path)], hashes)
            # A real resumption of THIS job still wins over an old correction.
            with FileLock(str(root/'jobs/job-fixture.lock'), timeout=0):
                self.assertEqual(corrected_status(root, job)['status'], 'running')
            run_path.write_text(json.dumps({**failure, 'message': 'changed'}))
            self.assertEqual(corrected_status(root, job)['status'], 'running')
