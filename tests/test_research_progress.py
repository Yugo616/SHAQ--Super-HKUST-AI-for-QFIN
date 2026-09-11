from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from shaq_daily_oracle.research_progress import ResearchProgressLog, safe_observe


class ResearchProgressLogTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
