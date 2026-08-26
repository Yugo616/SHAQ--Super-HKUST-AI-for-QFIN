from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.cli import build_parser  # noqa: E402
from shaq_daily_oracle.collectors import (  # noqa: E402
    CollectorError,
    build_capital_analysis,
    build_capital_document,
    build_derivatives_document,
    build_relationship_document,
    collection_status,
)
from shaq_daily_oracle.reports import write_reports  # noqa: E402
from shaq_daily_oracle.schedule import formal_mode, session_times  # noqa: E402
from shaq_daily_oracle.workflow import (  # noqa: E402
    Workflow,
    WorkflowError,
    _system_config_sha256,
)
from shaq_daily_oracle.canary import build_canary_intents  # noqa: E402


class CollectorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.captured = "2026-08-21T08:45:00-04:00"

    def test_capital_requires_event_direction_and_depth_and_is_order_invariant(self):
        ticks = [
            {"time": "08:44:02", "price": 101, "volume": 20, "ticker_direction": "BUY"},
            {"time": "08:44:01", "price": 100, "volume": 10, "ticker_direction": "SELL"},
        ]
        books = [{
            "observed_at_et": "2026-08-21T08:44:03-04:00",
            "bid": [{"price": 100, "volume": 100}],
            "ask": [{"price": 101, "volume": 50}],
        }]
        first = build_capital_document(
            symbol="AAPL", ticker_rows=ticks, order_book_samples=books,
            captured_at_et=self.captured,
        )
        second = build_capital_document(
            symbol="AAPL", ticker_rows=reversed(ticks), order_book_samples=reversed(books),
            captured_at_et=self.captured,
        )
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["metrics"]["signed_volume_imbalance"], 1 / 3)
        self.assertFalse(first["aggregate_vendor_money_flow_used"])
        raw = (json.dumps(first, sort_keys=True) + "\n").encode()
        view = json.loads(build_capital_analysis(raw))
        self.assertEqual(view["metrics"], first["metrics"])
        self.assertEqual(view["source_observation_counts"]["ticker_rows"], 2)
        self.assertNotIn("ticker_rows", view)
        self.assertLess(len(build_capital_analysis(raw)), len(raw))
        with self.assertRaises(CollectorError):
            build_capital_document(
                symbol="AAPL", ticker_rows=[], order_book_samples=books,
                captured_at_et=self.captured,
            )

    def test_options_describe_distribution_without_mechanical_direction(self):
        rows = [
            {"code": "C", "option_type": "CALL", "strike_time": "2026-09-18", "strike_price": 100,
             "bid_price": 4, "ask_price": 5, "option_implied_volatility": 30, "volume": 5000,
             "option_open_interest": 1},
            {"code": "P", "option_type": "PUT", "strike_time": "2026-09-18", "strike_price": 100,
             "bid_price": 3, "ask_price": 4, "option_implied_volatility": 35, "volume": 1,
             "option_open_interest": 9000},
        ]
        document = build_derivatives_document(
            symbol="AAPL", underlying_price=100, option_rows=rows,
            captured_at_et=self.captured,
        )
        self.assertFalse(document["directional_flow_eligible"])
        self.assertTrue(document["mechanical_put_call_or_oi_direction_forbidden"])
        self.assertAlmostEqual(document["expiries"]["2026-09-18"]["implied_move_fraction"], 0.08)

    def test_relationship_uses_pit_industry_and_126_session_exposure(self):
        stock = []
        sector = []
        for index in range(127):
            sector_close = 100 + index
            stock_close = 50 + 1.5 * index
            stock.append({"close": stock_close})
            sector.append({"close": sector_close})
        document = build_relationship_document(
            symbol="AAA",
            universe_rows=[
                {"ticker": "AAA", "gics_sector": "Industrials", "gics_sub_industry": "Machinery"},
                {"ticker": "BBB", "gics_sector": "Industrials", "gics_sub_industry": "Machinery"},
            ],
            price_history={"stock_bars": stock, "sector_bars": sector, "sector_benchmark": "XLI"},
            captured_at_et=self.captured, exposure_window=126,
        )
        self.assertEqual(document["same_subindustry_symbols"], ["BBB"])
        self.assertTrue(document["correlation_is_not_an_economic_relationship"])
        self.assertEqual(document["exposure_window_sessions"], 126)

    def test_collection_status_distinguishes_absence_from_provider_failure(self):
        no_data = collection_status(
            domain="event", symbol="AAPL", status="no_data",
            captured_at_et=self.captured, reason="no filing",
        )
        error = collection_status(
            domain="capital", symbol="AAPL", status="provider_error",
            captured_at_et=self.captured, reason="connection refused",
        )
        self.assertNotEqual(no_data["status"], error["status"])
        with self.assertRaises(CollectorError):
            collection_status(
                domain="capital", symbol="AAPL", status="collected",
                captured_at_et=self.captured, record_count=0,
            )

    def test_runtime_clock_has_separate_evidence_and_publication_deadlines(self):
        config = json.loads((ROOT / "config/runtime.json").read_text(encoding="utf-8"))
        schedule = session_times(date(2026, 8, 21), config)
        self.assertLess(schedule["evidence_cutoff"], schedule["forecast_deadline"])
        zone = ZoneInfo("America/New_York")
        self.assertEqual(formal_mode(datetime(2026, 8, 21, 8, 49, tzinfo=zone), schedule), "paper")
        self.assertEqual(formal_mode(datetime(2026, 8, 21, 8, 51, tzinfo=zone), schedule), "shadow")

    def test_single_entry_parser_is_the_public_operation(self):
        args = build_parser().parse_args(["run", "--mode", "paper"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.mode, "paper")
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('daily-oracle = "shaq_daily_oracle.cli:main"', project)

    def test_professor_views_are_hash_bound_to_frozen_run(self):
        frozen = {
            "run_id": "SHAQ-CANARY-2026-08-21-001", "mode": "shadow",
            "run_sha256": "a" * 64, "predictions": [], "reports_by_symbol": {},
            "adversary_by_symbol": {},
        }
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest = write_reports(runtime=root, frozen=frozen)
            self.assertEqual(manifest["frozen_run_sha256"], "a" * 64)
            self.assertTrue((root / "professor_report.html").is_file())
            self.assertTrue((root / "agent_trace.html").is_file())

    def test_stage_resume_does_not_repeat_completed_work(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            runtime.mkdir()
            workflow = Workflow(package_root=ROOT, runtime_root=runtime)
            output = runtime / "frozen.json"
            calls = []

            def action():
                calls.append("called")
                output.write_text("{}", encoding="utf-8")

            workflow._stage(runtime, "freeze", [output], action)
            workflow._stage(runtime, "freeze", [output], action)
            self.assertEqual(calls, ["called"])
            partial = runtime / "partial.json"
            partial.write_text("{}", encoding="utf-8")
            with self.assertRaises(WorkflowError):
                workflow._stage(runtime, "partial", [partial, runtime / "missing.json"], lambda: None)

    def test_system_identity_detects_code_drift_but_ignores_runtime_records(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "config").mkdir()
            (root / "runtime").mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            governed = root / "config" / "policy.json"
            governed.write_text('{"value":1}', encoding="utf-8")
            first = _system_config_sha256(root)
            (root / "runtime" / "record.json").write_text('{"result":1}', encoding="utf-8")
            self.assertEqual(first, _system_config_sha256(root))
            governed.write_text('{"value":2}', encoding="utf-8")
            self.assertNotEqual(first, _system_config_sha256(root))

    def test_provider_failure_creates_fail_closed_professor_record(self):
        with tempfile.TemporaryDirectory() as name:
            workflow = Workflow(package_root=ROOT, runtime_root=Path(name))
            runtime = workflow.failure_record(RuntimeError("provider unavailable"))
            failure = json.loads((runtime / "workflow_failure.json").read_text(encoding="utf-8"))
            self.assertFalse(failure["orders_submitted"])
            self.assertEqual(failure["status"], "fail_closed")
            self.assertTrue((runtime / "professor_report.html").is_file())

    def test_zero_forecast_paper_run_completes_without_waiting_for_close(self):
        zone = ZoneInfo("America/New_York")
        observed = datetime(2026, 8, 26, 8, 52, tzinfo=zone)
        trade_date = observed.date()
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            sleeps = []
            workflow = Workflow(
                package_root=ROOT,
                runtime_root=runtime,
                now=lambda: observed,
                sleep=lambda seconds: sleeps.append(seconds),
            )
            frozen_path = runtime / "frozen_run.json"
            portfolio = runtime / "portfolio_snapshot.json"
            intents = runtime / "order_intents.json"
            availability = runtime / "collection_availability.json"
            frozen_path.write_text(json.dumps({
                "run_id": "SHAQ-CANARY-2026-08-26-001",
                "mode": "canary",
                "run_sha256": "a" * 64,
                "predictions": [],
                "reports_by_symbol": {},
                "adversary_by_symbol": {},
            }), encoding="utf-8")
            portfolio.write_text(json.dumps({
                "trd_env": "SIMULATE", "positions": [],
            }), encoding="utf-8")
            intents.write_text(json.dumps({
                "run_id": "SHAQ-CANARY-2026-08-26-001", "intents": [],
            }), encoding="utf-8")
            availability.write_text("{}", encoding="utf-8")

            def fake_script(name: str, *arguments: str) -> None:
                output = Path(arguments[arguments.index("--output") + 1])
                if name == "build_execution_ledger.py":
                    payload = {
                        "schema_version": 6,
                        "run_id": "SHAQ-CANARY-2026-08-26-001",
                        "trd_env": "SIMULATE",
                        "scientific_labels_are_separate": True,
                        "round_trips": [],
                    }
                elif name == "evaluate_readiness.py":
                    payload = {
                        "probability": {
                            "probability_publication_allowed": False,
                            "p_committee_hit": None,
                        },
                        "cost": {},
                        "net_profit": {
                            "net_profit_publication_allowed": False,
                            "p_net_profit": None,
                        },
                    }
                elif name == "audit_canary.py":
                    payload = {"status": "passed", "stage": "complete"}
                else:
                    raise AssertionError(name)
                output.write_text(json.dumps(payload), encoding="utf-8")

            workflow._script = fake_script  # type: ignore[method-assign]
            workflow._complete_no_trade(
                runtime=runtime,
                trade_date=trade_date,
                schedule=session_times(trade_date, workflow.runtime_config),
                frozen_path=frozen_path,
                portfolio=portfolio,
                intents=intents,
                journal=runtime / "broker_journal.json",
                ledger_view=runtime / "execution_ledger.json",
                provisional_view=runtime / "labels_provisional.json",
                availability_path=availability,
            )
            self.assertEqual(sleeps, [])
            self.assertTrue((runtime / "audit_complete.json").is_file())
            self.assertEqual(
                json.loads((runtime / "broker_journal.json").read_text(encoding="utf-8"))["run_status"],
                "NO_TRADE",
            )
            labels = json.loads((runtime / "labels_provisional.json").read_text(encoding="utf-8"))
            self.assertEqual(labels["official_label_status"], "not_applicable_no_forecasts")

    def test_shadow_forecast_never_becomes_ready_order(self):
        policy = {
            "run_id": "R", "created_at": self.captured, "intent_created_at": self.captured,
            "forecast_cutoff": "2026-08-21T09:00:00-04:00",
            "entry_after": "2026-08-21T09:30:00-04:00",
            "entry_deadline": "2026-08-21T09:35:00-04:00",
            "exit_at": "2026-08-21T15:55:00-04:00", "exit_deadline": "2026-08-21T15:58:00-04:00",
            "forecast_mode": "shadow", "trd_env": "SIMULATE", "real_trading_enabled": False,
            "max_forecasts": 3, "shares_per_forecast": 1,
            "account_allowlist": ["US_SIMULATE_CANARY"],
            "portfolio_observed_at": self.captured, "borrow_captured_at": self.captured,
            "max_portfolio_age_seconds": 300, "max_borrow_age_seconds": 300,
        }
        result = build_canary_intents(
            forecasts=[{"symbol": "AAPL", "direction": "bullish", "score_eligible": True}],
            portfolio={"trd_env": "SIMULATE", "account_alias": "US_SIMULATE_CANARY", "positions": []},
            borrowable={}, policy=policy,
        )
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["intents"][0]["status"], "SHADOW_ONLY")


if __name__ == "__main__":
    unittest.main()
