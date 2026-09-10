from __future__ import annotations

import json
import unittest
from pathlib import Path

from shaq_daily_oracle.decision_sandbox import (
    DecisionSandboxError,
    build_decision_input,
    execute_decision_script,
    run_decision_cases,
)


ROOT = Path(__file__).resolve().parents[1]


def _report(domain: str, verdict: str, roots: list[str]) -> dict:
    return {
        "domain": domain,
        "availability": "available",
        "verdict": verdict,
        "lineage_root_ids": roots,
    }


class DecisionSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reports = {
            "AAA": [
                _report("market", "bullish", ["root-market"]),
                _report("price_volume", "bullish", ["root-stock"]),
            ],
            "BBB": [
                _report("market", "bullish", ["root-market"]),
                _report("price_volume", "bearish", ["root-bbb"]),
            ],
        }
        self.adversary = {
            "AAA": {"veto": False, "strongest_countercase": "可能已经充分定价"},
            "BBB": {"veto": False, "strongest_countercase": "证据方向冲突"},
        }
        self.candidates = {
            "candidates": [
                {"symbol": "AAA", "gics_sector": "Technology", "captured_primary_event": False},
                {"symbol": "BBB", "gics_sector": "Health Care", "captured_primary_event": True},
            ]
        }
        self.root_types = {
            "root-market": ["market_context"],
            "root-stock": ["stock_price_volume"],
            "root-bbb": ["stock_price_volume"],
        }

    def _input(self) -> dict:
        return build_decision_input(
            as_of_et="2026-09-08T08:50:00-04:00",
            reports_by_symbol=self.reports,
            adversary_by_symbol=self.adversary,
            candidate_intake=self.candidates,
            root_component_types=self.root_types,
        )

    def test_bundled_decision_rule_reproduces_safe_publish_and_rejection(self) -> None:
        script = (ROOT / "decision/decision.js").read_text(encoding="utf-8")
        result = execute_decision_script(script=script, decision_input=self._input())
        self.assertEqual(result["predictions"], [{
            "symbol": "AAA",
            "direction": "bullish",
            "track": "ordinary",
            "score_eligible": True,
            "industry_group": "Technology",
        }])
        by_symbol = {row["symbol"]: row for row in result["decisions"]}
        self.assertEqual(by_symbol["AAA"]["action"], "publish")
        self.assertEqual(by_symbol["BBB"]["action"], "reject")
        self.assertEqual(result["engine"], "quickjs-isolated")

    def test_sandbox_rejects_future_result_fields_and_invented_roots(self) -> None:
        tainted = self._input()
        tainted["labels"] = {"AAA": {"actual_direction": "bullish"}}
        with self.assertRaisesRegex(DecisionSandboxError, "forbidden"):
            execute_decision_script(
                script=(ROOT / "decision/decision.js").read_text(encoding="utf-8"),
                decision_input=tainted,
            )
        script = """
        function decide(input) {
          return {schema_version: 1, decisions: input.candidates.map(c => ({
            symbol: c.symbol, action: c.symbol === 'AAA' ? 'publish' : 'reject',
            direction: c.symbol === 'AAA' ? 'bullish' : 'neutral',
            reason: 'test', evidence_root_ids: c.symbol === 'AAA' ? ['invented'] : []
          }))};
        }
        """
        with self.assertRaisesRegex(DecisionSandboxError, "root"):
            execute_decision_script(script=script, decision_input=self._input())

    def test_sandbox_stops_infinite_code_and_has_no_host_api(self) -> None:
        with self.assertRaisesRegex(DecisionSandboxError, "time|interrupted"):
            execute_decision_script(
                script="function decide(input) { while (true) {} }",
                decision_input=self._input(),
            )
        no_host = """
        function decide(input) {
          const isolated = typeof process === 'undefined' && typeof require === 'undefined'
            && typeof fetch === 'undefined' && typeof XMLHttpRequest === 'undefined';
          return {schema_version: 1, decisions: input.candidates.map(c => ({
            symbol: c.symbol, action: 'reject', direction: 'neutral',
            reason: isolated ? 'isolated' : 'host-visible', evidence_root_ids: []
          }))};
        }
        """
        result = execute_decision_script(script=no_host, decision_input=self._input())
        self.assertTrue(all(row["reason"] == "isolated" for row in result["decisions"]))

    def test_bundled_cases_are_executable_and_hash_bound(self) -> None:
        script = (ROOT / "decision/decision.js").read_text(encoding="utf-8")
        cases = (ROOT / "decision/cases.json").read_text(encoding="utf-8")
        receipt = run_decision_cases(script=script, cases_text=cases)
        self.assertEqual(receipt["status"], "passed")
        self.assertGreaterEqual(receipt["case_count"], 2)
        self.assertEqual(len(receipt["script_sha256"]), 64)
        self.assertEqual(len(receipt["cases_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
