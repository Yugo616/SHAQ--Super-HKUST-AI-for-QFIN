from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.hashing import sha256_file, sha256_payload  # noqa: E402
from shaq_daily_oracle.replay import run_replay, verify_replay_inputs, write_run_replay  # noqa: E402


class RunReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        intake = {
            "candidates": [
                {
                    "symbol": "DLTR", "sector_benchmark": "XLP",
                    "stock_premarket_return": -0.01, "sector_premarket_return": -0.005,
                    "residual_premarket_return": -0.005, "premarket_volume": 8000,
                },
                {
                    "symbol": "DG", "sector_benchmark": "XLP", "gics_sector": "Consumer Staples",
                    "stock_premarket_return": 0.0605, "sector_premarket_return": -0.0058,
                    "residual_premarket_return": 0.0663, "premarket_volume": 156559,
                },
            ]
        }
        intake_path = self.runtime / "candidate_intake.json"
        intake_path.write_text(json.dumps(intake, sort_keys=True), encoding="utf-8")
        neutral = {
            "as_of_et": "2026-08-27T08:50:00-04:00",
            "horizon": "official_US_regular_session_open_to_close",
            "availability": "available", "verdict": "neutral",
            "thesis": "Evidence does not bridge the premarket move to the regular session.",
            "antithesis": "The move could continue after the open.",
            "unknowns": ["opening auction"], "invalidation": ["new primary evidence"],
            "evidence_ids": [], "lineage_root_ids": [],
        }
        reports = {}
        for symbol in ("DG", "DLTR"):
            reports[symbol] = []
            for domain in ("market", "relationships", "event", "capital", "derivatives", "price_volume"):
                report = dict(neutral, domain=domain)
                if symbol == "DG" and domain == "capital":
                    report.update(
                        verdict="bearish",
                        thesis="Sustained premarket selling pressure accompanied a price decline.",
                        antithesis="The pressure may be temporary inventory rebalancing.",
                    )
                reports[symbol].append(report)
        payload = {
            "schema_version": 6,
            "run_id": "SHAQ-CANARY-2026-08-27-001",
            "mode": "canary",
            "cutoff_et": "2026-08-27T08:50:00-04:00",
            "created_at": "2026-08-27T08:59:00-04:00",
            "candidate_intake_sha256": sha256_file(intake_path),
            "predictions": [],
            "reports_by_symbol": reports,
            "adversary_by_symbol": {
                symbol: {"veto": False, "strongest_countercase": "The premarket move may continue.", "unresolved_conflicts": []}
                for symbol in reports
            },
            "integration_audit_by_symbol": {
                "DG": {"published": False, "directional_domain_count": 1, "rejection_reasons": ["fewer_than_two_directional_domains"]},
                "DLTR": {"published": False, "directional_domain_count": 0, "rejection_reasons": ["fewer_than_two_directional_domains"]},
            },
            "lineage": {"records": [], "root_component_types": {}},
        }
        payload["run_sha256"] = sha256_payload(payload)
        self.frozen = payload

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_replay_shows_all_candidates_and_selects_most_directional(self):
        page = run_replay(runtime=self.runtime, frozen=self.frozen)
        self.assertIn('data-audit-status="passed"', page)
        self.assertIn('data-default-symbol="DG"', page)
        self.assertEqual(page.count('data-symbol="'), 2)
        self.assertIn("少于两个领域给出同向判断", page)
        self.assertIn("Sustained premarket selling pressure", page)
        self.assertNotIn("盘后核验｜盘前不可见", page)

    def test_post_close_result_is_shown_only_when_present(self):
        postmortem = self.runtime / "postmortem"
        postmortem.mkdir()
        (postmortem / "outcomes_final.json").write_text(json.dumps({
            "captured_at_et": "2026-08-28T08:00:00-04:00",
            "rows_by_symbol": {
                "DG": {"official_open": 127.84, "official_close": 125.89,
                       "official_open_to_close_return": -0.015253, "actual_direction": "bearish"}
            },
        }), encoding="utf-8")
        page = run_replay(runtime=self.runtime, frozen=self.frozen)
        self.assertIn("盘后核验｜盘前不可见", page)
        self.assertIn("-1.53%", page)
        self.assertIn("系统没有发布预测，因此不记为命中", page)

    def test_tampered_frozen_run_fails_closed(self):
        broken = dict(self.frozen)
        broken["mode"] = "shadow"
        page = run_replay(runtime=self.runtime, frozen=broken)
        self.assertIn('data-audit-status="failed"', page)
        self.assertNotIn("Sustained premarket selling pressure", page)

    def test_tampered_candidate_file_fails_verification(self):
        (self.runtime / "candidate_intake.json").write_text('{"candidates":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_replay_inputs(self.runtime, self.frozen)

    def test_written_replay_has_hash_manifest_and_no_local_username(self):
        manifest = write_run_replay(runtime=self.runtime, frozen=self.frozen)
        page = (self.runtime / "run_replay.html").read_text(encoding="utf-8")
        self.assertEqual(manifest["status"], "passed")
        self.assertEqual(manifest["run_replay_sha256"], sha256_file(self.runtime / "run_replay.html"))
        self.assertNotIn("/Users/", page)


if __name__ == "__main__":
    unittest.main()
