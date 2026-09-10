from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shaq_daily_oracle.lab_service import LabService


class ResultRefreshTests(unittest.TestCase):
    def service(self, root: Path) -> LabService:
        self.assertTrue(hasattr(LabService, "start_result_refresh"),
                        "Lab service needs an asynchronous result refresh operation")
        service = LabService.__new__(LabService)
        service.paths = SimpleNamespace(
            research_root=root, batches_root=root / "batches",
        )
        service.settings = SimpleNamespace(load=lambda: {"data_profile": {
            "profile_id": "fixture", "universe_file": "unused.csv",
            "market_provider": "yfinance",
        }})
        return service

    def wait_status(self, service: LabService, expected: set[str]) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = service.result_refresh_status()
            if value.get("status") in expected:
                return value
            time.sleep(0.01)
        self.fail(f"refresh did not reach {expected}: {service.result_refresh_status()}")

    def test_manual_and_auto_refresh_are_single_flight_across_instances(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "batches").mkdir()
            entered, release = threading.Event(), threading.Event()

            def labels(**kwargs):
                entered.set()
                release.wait(2)
                return {"refreshed_batches": ["LAB-fixture"], "failures": []}

            first, second = self.service(root), self.service(root)
            with patch("shaq_daily_oracle.lab_service.refresh_research_labels", side_effect=labels), \
                    patch.object(LabService, "_refresh_minute_accounts", return_value={
                        "refreshed_dates": ["2026-09-09"], "failures": [],
                    }):
                started = first.start_result_refresh(manual=True)
                self.assertTrue(entered.wait(1))
                competing = second.start_result_refresh(manual=False)
                self.assertEqual(started["status"], "running")
                self.assertEqual(competing["status"], "already_running")
                self.assertEqual(started["operation_id"], competing["operation_id"])
                release.set()
                completed = self.wait_status(first, {"complete", "partial_failure", "failed"})
            self.assertEqual(completed["status"], "complete")

    def test_auto_honors_fifteen_minute_cadence_but_manual_can_retry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "batches").mkdir()
            service = self.service(root)
            receipt = root / "label_refresh_status.json"
            receipt.write_text(json.dumps({
                "status": "failed", "operation_id": "prior",
                "attempted_at": "2099-01-01T09:00:00-05:00",
            }), encoding="utf-8")
            self.assertEqual(service.start_result_refresh(manual=False)["status"], "not_due")
            with patch("shaq_daily_oracle.lab_service.refresh_research_labels", return_value={
                    "refreshed_batches": [], "failures": [{"batch_id": "LAB-x", "message": "offline"}],
                 }), patch.object(LabService, "_refresh_minute_accounts", return_value={
                    "refreshed_dates": [], "failures": [],
                 }):
                self.assertEqual(service.start_result_refresh(manual=True)["status"], "running")
                completed = self.wait_status(service, {"partial_failure", "failed"})
            self.assertEqual(completed["status"], "partial_failure")
            self.assertEqual(completed["result"]["failures"][0]["message"], "offline")

    def test_desktop_bridge_exposes_async_refresh_without_model_arguments(self):
        from shaq_daily_oracle.desktop import DesktopBridge

        self.assertTrue(hasattr(DesktopBridge, "refresh_prices_and_results"),
                        "Desktop bridge must expose the manual refresh operation")
        bridge = DesktopBridge.__new__(DesktopBridge)
        bridge.lab = SimpleNamespace(start_result_refresh=lambda **kwargs: {
            "status": "running", "manual": kwargs["manual"],
        })
        value = bridge.refresh_prices_and_results()
        self.assertEqual(value, {"ok": True, "value": {"status": "running", "manual": True}})

    def test_stale_running_receipt_does_not_block_manual_retry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "batches").mkdir()
            service = self.service(root)
            (root / "label_refresh_status.json").write_text(json.dumps({
                "status": "running", "operation_id": "crashed-process",
                "attempted_at": "2026-09-01T09:00:00-04:00",
            }), encoding="utf-8")
            with patch("shaq_daily_oracle.lab_service.refresh_research_labels", return_value={
                    "refreshed_batches": [], "failures": [],
                 }), patch.object(LabService, "_refresh_minute_accounts", return_value={
                    "refreshed_dates": [], "failures": [],
                 }):
                started = service.start_result_refresh(manual=True)
                self.assertEqual(started["status"], "running")
                self.assertNotEqual(started["operation_id"], "crashed-process")
                self.assertEqual(self.wait_status(service, {"complete"})["status"], "complete")

    def test_missing_daily_price_finishes_as_partial_failure_not_complete(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "batches").mkdir()
            service = self.service(root)
            missing = {"batch_id": "LAB-missing", "error_type": "MissingDailyPrice",
                       "message": "missing daily price: AAPL", "missing_symbols": ["AAPL"]}
            with patch("shaq_daily_oracle.lab_service.refresh_research_labels", return_value={
                    "refreshed_batches": [], "failures": [missing],
                 }), patch.object(LabService, "_refresh_minute_accounts", return_value={
                    "refreshed_dates": [], "failures": [],
                 }):
                service.start_result_refresh(manual=True)
                result = self.wait_status(service, {"complete", "partial_failure", "failed"})
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["result"]["failures"][0]["missing_symbols"], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
