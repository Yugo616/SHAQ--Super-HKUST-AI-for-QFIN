from __future__ import annotations

import json
import errno
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, time as clock_time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.campaign import (  # noqa: E402
    CampaignConfig,
    CampaignAlreadyRunning,
    CampaignError,
    Heartbeat,
    _write,
    campaign_lock,
    campaign_rows,
    write_campaign_views,
)
from shaq_daily_oracle.execution import phase_is_terminal  # noqa: E402
from shaq_daily_oracle.reports import professor_report  # noqa: E402


class CampaignServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def config(self, dates: list[str]) -> CampaignConfig:
        path = self.root / "campaign.json"
        path.write_text(json.dumps({
            "campaign_id": "test-campaign",
            "session_dates": dates,
            "runtime_root": str(self.root / "runtime"),
            "workflow_start_et": "08:20:00",
            "heartbeat_seconds": 30,
            "active_health_seconds": 60,
            "idle_health_seconds": 300,
            "opend_host": "127.0.0.1",
            "opend_port": 11111,
        }), encoding="utf-8")
        return CampaignConfig.load(path)

    def test_campaign_dates_are_explicit_weekdays(self):
        value = self.config(["2026-08-25", "2026-08-26"])
        self.assertEqual(len(value.session_dates), 2)
        with self.assertRaises(CampaignError):
            self.config(["2026-08-29"])
        with self.assertRaises(CampaignError):
            self.config(["2026-08-25", "2026-08-25"])
        with self.assertRaises(CampaignError):
            self.config(["2026-09-07"])

    def test_duplicate_service_lock_fails_closed(self):
        lock_path = self.root / "service.lock"
        with campaign_lock(lock_path):
            with self.assertRaises(CampaignError):
                with campaign_lock(lock_path):
                    pass

    def test_macos_deadlock_lock_is_treated_as_benign_duplicate(self):
        with patch(
            "shaq_daily_oracle.campaign.fcntl.flock",
            side_effect=OSError(errno.EDEADLK, "Resource deadlock avoided"),
        ):
            with self.assertRaises(CampaignAlreadyRunning):
                with campaign_lock(self.root / "deadlock.lock"):
                    pass

    def test_atomic_status_writes_remain_valid_under_competing_writers(self):
        path = self.root / "status.json"

        def writer(identity: int) -> None:
            for sequence in range(20):
                _write(path, {"writer": identity, "sequence": sequence})

        threads = [threading.Thread(target=writer, args=(identity,)) for identity in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn(value["writer"], range(4))
        self.assertEqual(list(self.root.glob(".status.json.*.tmp")), [])

    def test_heartbeat_records_unreachable_opend_without_claiming_health(self):
        config = CampaignConfig(
            campaign_id="heartbeat-test", session_dates=(date(2026, 8, 25),),
            runtime_root=self.root / "heartbeat", workflow_start_et=clock_time(8, 20),
            heartbeat_seconds=1, active_health_seconds=1, idle_health_seconds=1,
            host="127.0.0.1", port=1,
        )
        heartbeat = Heartbeat(config, session_date=date(2026, 8, 25))
        heartbeat.start()
        deadline = time.monotonic() + 2
        status_path = config.runtime_root / "service_status.json"
        while not status_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        heartbeat.close()
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertFalse(status["opend_reachable"])
        self.assertEqual(status["stage"], "starting")

    def test_phase_terminal_requires_every_symbol_and_accepts_zero_fill_exit(self):
        orders = {
            "a:entry": {"status": "FILLED", "reconciliation_status": "reconciled", "dealt_qty": 1},
            "b:entry": {"status": "CANCELLED", "reconciliation_status": "reconciled", "dealt_qty": 0},
            "a:exit": {"status": "FILLED", "reconciliation_status": "reconciled", "dealt_qty": 1},
        }
        self.assertTrue(phase_is_terminal(orders, ["a", "b"], "exit"))
        self.assertTrue(phase_is_terminal(orders, ["a", "b"], "entry"))
        self.assertFalse(phase_is_terminal(orders, ["a", "b", "c"], "entry"))

    def test_campaign_views_score_only_canary_records(self):
        config = self.config(["2026-08-25", "2026-08-26"])
        runtime = config.runtime_root / "SHAQ-CANARY-2026-08-25-001"
        runtime.mkdir(parents=True)
        (runtime / "frozen_run.json").write_text(json.dumps({
            "mode": "shadow", "predictions": [{"symbol": "AAPL"}],
        }), encoding="utf-8")
        write_campaign_views(config)
        rows = campaign_rows(config)
        self.assertEqual(rows[0]["正式预测数"], 0)
        self.assertIn("连续模拟进度", (config.runtime_root / "campaign_status.html").read_text(encoding="utf-8"))
        self.assertTrue((config.runtime_root / "campaign_summary.csv").is_file())

    def test_professor_report_uses_plain_chinese(self):
        page = professor_report(
            frozen={"run_id": "r", "mode": "canary", "run_sha256": "a" * 64, "predictions": []},
            collection_statuses=None, orders={"orders": []}, labels={"labels": []},
        )
        self.assertIn("今天没有股票同时满足证据门槛", page)
        self.assertIn("交易账和预测成绩分开保存", page)

    def test_order_polling_is_registered_at_fifteen_seconds(self):
        runtime = json.loads((ROOT / "config/runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["order_poll_interval_seconds"], 15)
        self.assertEqual(
            runtime["parameter_bindings"]["order_poll_interval_seconds"],
            ["REF-FUTU-ORDER-LIST-001", "DEC-ORDER-POLL-001", "EXP-CANARY-001"],
        )


if __name__ == "__main__":
    unittest.main()
