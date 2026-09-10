from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class LabSmokeTests(unittest.TestCase):
    def test_two_method_fixture_runs_zipline_settlement_and_reopens(self) -> None:
        from shaq_daily_oracle.lab_smoke import run_lab_smoke

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            had_backtrader = "backtrader" in sys.modules
            value = run_lab_smoke(package_root=PACKAGE_ROOT, output_root=root)

        self.assertEqual(value["status"], "passed")
        self.assertEqual(
            [(row["method_name"], row["status_badge"]) for row in value["methods"]],
            [("独立证据门禁版", "正式基准"), ("跨域综合研判版", "Shadow")],
        )
        self.assertEqual(set(value["variant_keys"]), {
            "team/independent-gate-1", "team/cross-domain-synthesis-1",
        })
        self.assertEqual(len(set(value["evidence_hashes"].values())), 1)
        self.assertTrue(value["method_comparison"]["changed_file_count"] > 0)
        self.assertNotEqual(
            value["method_comparison"]["decision_mode"]["left"],
            value["method_comparison"]["decision_mode"]["right"],
        )
        self.assertEqual(value["account_engine"], "zipline-reloaded")
        self.assertEqual(value["account_engine_version"], "3.1.1")
        self.assertEqual(set(value["provisional_statuses"]), {"provisional"})
        self.assertEqual(set(value["final_statuses"]), {"final"})
        self.assertTrue(value["reopen_matches"])
        self.assertFalse(value["provider_secrets_used"])
        self.assertFalse(value["broker_modules_loaded"])
        self.assertEqual("backtrader" in sys.modules, had_backtrader)
        contract = value["account_contract"]
        self.assertEqual(contract["rules"], {
            "initial_cash": 10000, "per_prediction_budget": 1000,
            "commission_rate": 0.0005, "slippage_rate": 0.0005,
            "schema_version": 2,
        })
        self.assertEqual(contract["entry_reference_at_et"], "2026-09-09T09:31:00-04:00")
        self.assertEqual(contract["exit_reference_at_et"], "2026-09-09T15:55:00-04:00")
        self.assertEqual(contract["exit_minutes_before_close"], 5)
        long_trade = contract["long"]
        self.assertEqual((long_trade["direction"], long_trade["quantity"]), ("bullish", 9))
        self.assertIs(type(long_trade["quantity"]), int)
        self.assertEqual((long_trade["entry_reference_open"], long_trade["exit_reference_open"]), (100.0, 102.0))
        self.assertAlmostEqual(long_trade["entry_price"], 100.05)
        self.assertAlmostEqual(long_trade["exit_price"], 101.949)
        self.assertAlmostEqual(long_trade["gross_pnl"], 18.0)
        self.assertAlmostEqual(long_trade["fees"], 0.9089955)
        self.assertAlmostEqual(long_trade["slippage_cost"], 0.909)
        self.assertAlmostEqual(long_trade["net_pnl"], 16.1820045)
        self.assertAlmostEqual(long_trade["closing_cash"], 10016.1820045)
        reserved_short = contract["reserved_short"]
        self.assertEqual((reserved_short["direction"], reserved_short["quantity"]), ("bearish", 9))
        self.assertIs(type(reserved_short["quantity"]), int)
        self.assertEqual((reserved_short["entry_reference_open"], reserved_short["exit_reference_open"]), (100.0, 102.0))
        self.assertAlmostEqual(reserved_short["entry_price"], 99.95)
        self.assertAlmostEqual(reserved_short["exit_price"], 102.051)
        self.assertAlmostEqual(reserved_short["reserved_collateral"], 900.900225)
        self.assertAlmostEqual(reserved_short["gross_pnl"], -18.0)
        self.assertAlmostEqual(reserved_short["fees"], 0.9090045)
        self.assertAlmostEqual(reserved_short["slippage_cost"], 0.909)
        self.assertAlmostEqual(reserved_short["net_pnl"], -19.8180045)
        self.assertAlmostEqual(reserved_short["closing_cash"], 9980.1819955)
        self.assertTrue(long_trade["reconciled"])
        self.assertTrue(reserved_short["reconciled"])
        self.assertTrue(all(value["contract_checks"].values()))
        self.assertEqual(
            {case["name"]: case["status"] for case in value["view_cases"]},
            {
                "long": "final", "empty": "empty", "pending": "pending",
                "incomplete": "incomplete", "failed": "error",
            },
        )

    def test_desktop_smoke_runs_whole_lab_fixture_without_legacy_engine_gate(self) -> None:
        from contextlib import redirect_stdout
        from shaq_daily_oracle import desktop

        output = io.StringIO()
        with redirect_stdout(output):
            code = desktop.main(["--smoke"])
        value = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "passed")
        self.assertTrue(value["checks"]["whole_lab_fixture"])
        self.assertTrue(value["checks"]["zipline_minute_engine"])
        self.assertTrue(value["checks"]["two_canonical_methods"])
        self.assertTrue(value["checks"]["view_states"])
        self.assertTrue(value["checks"]["accounting_contract"])
        self.assertNotIn("backtrader_license", value["checks"])
        self.assertNotIn("virtual_account_engine", value["checks"])
        long_case = next(row for row in value["view_cases"] if row["name"] == "long")
        self.assertTrue(long_case["trades"])
        self.assertTrue(all(order["status"] == "Completed" for order in long_case["orders"]))
        self.assertEqual(long_case["trades"][0]["entry_reference_at_et"][11:16], "09:31")
        self.assertTrue(value["browser_state"]["dashboard"]["virtual_accounts"]["results"])
        self.assertIn("batch_id", value["batch_detail"])
        serialized = json.dumps(value).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("fixture-secret", serialized)


if __name__ == "__main__":
    unittest.main()
