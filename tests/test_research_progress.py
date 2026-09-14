from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from shaq_daily_oracle.research_progress import ResearchProgressLog, safe_observe


class ResearchProgressLogTests(unittest.TestCase):
    def test_completed_and_failed_resume_jobs_and_events_survive_service_reload(self):
        from shaq_daily_oracle.lab_service import LabService
        def service(root):
            value=LabService.__new__(LabService)
            value.paths=SimpleNamespace(research_root=root)
            value.jobs={};value.jobs_lock=threading.Lock()
            value.dashboard=SimpleNamespace(batch_detail=lambda batch_id:{'batch_id':batch_id},
                overview=lambda:{'virtual_accounts':{'results':[]}})
            return value
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=service(root);saved={}
            for suffix,status in [('aaaaaaaaaaaa','complete'),('bbbbbbbbbbbb','failed')]:
                batch_id='LAB-2026-09-11-'+suffix;job_id='resume-'+batch_id
                first.jobs[job_id]={'job_id':job_id,'batch_id':batch_id}
                first._set_job(job_id,status=status,started_at_et='2026-09-15T08:00:00-04:00',
                    completed_at_et='2026-09-15T08:01:00-04:00')
                event=ResearchProgressLog(root/'jobs'/f'{job_id}-research.jsonl').append(
                    stage='decision_complete' if status=='complete' else 'failure',
                    batch_id=batch_id,variant_key='team/main',status=status)
                saved[job_id]=(batch_id,status,event,(root/'jobs'/f'{job_id}.json').read_bytes())
            # Preserve the original job namespace and ignore mismatched resume file identities.
            (root/'jobs/job-historical.json').write_text(json.dumps({'job_id':'job-historical','status':'complete'}))
            (root/'jobs/resume-LAB-forged.json').write_text(json.dumps({'job_id':'../../other','batch_id':'LAB-forged','status':'complete'}))
            (root/'jobs/resume-LAB-mismatch.json').write_text(json.dumps({'job_id':'resume-LAB-mismatch','batch_id':'LAB-other','status':'failed'}))
            restarted=service(root);rows={row['job_id']:row for row in restarted.job_statuses()}
            self.assertEqual(set(rows),set(saved)|{'job-historical'})
            for job_id,(batch_id,status,event,original) in saved.items():
                self.assertEqual(rows[job_id]['status'],status)
                self.assertEqual(rows[job_id]['research_progress'],[event])
                self.assertEqual(restarted.batch_detail(batch_id)['research_progress'],[event])
                self.assertEqual((root/'jobs'/f'{job_id}.json').read_bytes(),original)

    def test_append_is_incremental_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.jsonl"
            first = ResearchProgressLog(path, clock=lambda: "2026-09-11T08:00:00-04:00")
            start = first.append(stage="call_requested", batch_id="B", variant_key="team/main",
                         symbols=["AAPL", "MSFT"], domain="market", call_id="call-1",
                         attempt=1)
            second = ResearchProgressLog(path, clock=lambda: "2026-09-11T08:00:03-04:00")
            second.append(stage="model_returned", batch_id="B", variant_key="team/main",
                          symbols=["AAPL", "MSFT"], domain="market", call_id="call-1",
                          attempt=start["attempt"], status="complete", elapsed_seconds=3.0)

            rows = second.read()
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertEqual(rows[0]["symbols"], ["AAPL", "MSFT"])
            self.assertEqual(rows[0]["occurred_at_et"], "2026-09-11T08:00:00-04:00")
            self.assertEqual(rows[1]["elapsed_seconds"], 3.0)
            self.assertEqual(rows[0]["attempt"], rows[1]["attempt"])

    def test_locked_sidecar_returns_immediately(self):
        from filelock import FileLock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.jsonl"
            lock = FileLock(str(path) + ".lock"); lock.acquire()
            started = time.monotonic()
            self.assertIsNone(ResearchProgressLog(path).append(stage="preparation"))
            self.assertLess(time.monotonic() - started, 0.1)
            lock.release()

    def test_bad_observer_and_unreadable_tail_are_nonblocking(self):
        safe_observe(lambda **event: (_ for _ in ()).throw(OSError("disk")), stage="preparation")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.jsonl"
            log = ResearchProgressLog(path)
            log.append(stage="preparation", batch_id="B")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
            self.assertEqual(len(ResearchProgressLog(path).read()), 1)

    def test_event_rejects_raw_model_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = ResearchProgressLog(Path(tmp) / "progress.jsonl")
            with self.assertRaises(ValueError):
                log.append(stage="validation_failure", batch_id="B", raw_output={"thesis": "unsafe"})

    def test_truncated_multibyte_tail_keeps_valid_events_and_service_refreshes(self):
        from shaq_daily_oracle.lab_service import LabService
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); jobs = root / "jobs"; jobs.mkdir()
            job_id = "job-truncated"
            (jobs / f"{job_id}.json").write_text(json.dumps({
                "job_id": job_id, "status": "running",
                "started_at_et": "2026-09-11T08:00:00-04:00",
            }), encoding="utf-8")
            progress = jobs / f"{job_id}-research.jsonl"
            good = {"sequence": 1, "stage": "preparation", "message": "准备完成"}
            progress.write_bytes((json.dumps(good, ensure_ascii=False) + "\n").encode("utf-8")
                                 + '{"sequence":2,"message":"中文'.encode("utf-8")[:-1])
            service = LabService.__new__(LabService)
            service.paths = SimpleNamespace(research_root=root)
            service.jobs = {}; service.jobs_lock = threading.Lock()

            rows = service.job_statuses()
            self.assertEqual(rows[0]["research_progress"], [good])


if __name__ == "__main__":
    unittest.main()
