from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.market_calendar import (
    market_session,
    next_market_session,
    previous_market_session,
)
from shaq_daily_oracle.app_paths import AppPaths
from shaq_daily_oracle.dashboard import DashboardIndex
from shaq_daily_oracle.desktop import DesktopBridge
from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.schedule import session_times
from shaq_daily_oracle.sandboxed_codex import (
    SandboxedCodexError,
    _inference_call,
    _openai_call,
    attest_openai_responses,
    probe_ai_backend,
)
from shaq_daily_oracle.workflow import Workflow
from shaq_daily_oracle.settings import SettingsStore
from shaq_daily_oracle.service import run_worker


ROOT = Path(__file__).resolve().parents[1]


class DesktopFoundationTests(unittest.TestCase):
    def paths(self, root: Path) -> AppPaths:
        return AppPaths(
            package_root=ROOT,
            data_root=root / "data",
            config_root=root / "config",
            log_root=root / "logs",
            runtime_root=root / "data/runtime",
            dashboard_db=root / "data/dashboard.sqlite3",
            settings_file=root / "config/settings.json",
            effective_ai_config=root / "config/ai-backend.json",
        ).ensure()

    @staticmethod
    def ai_config() -> dict:
        names = (
            "backend", "model", "reasoning_effort", "timeout_seconds",
            "maximum_input_bytes", "maximum_output_tokens",
        )
        value = {
            "backend": "openai-responses", "model": "gpt-test",
            "reasoning_effort": "high", "timeout_seconds": 30,
            "maximum_input_bytes": 1000, "maximum_output_tokens": 500,
        }
        value["parameter_bindings"] = {
            name: ["REF", "DEC", "EXP"] for name in names
        }
        return value

    def test_weekend_and_nyse_holiday_have_a_next_session(self) -> None:
        self.assertIsNone(market_session(date(2026, 8, 29)))
        self.assertEqual(next_market_session(date(2026, 8, 29)).session_date, date(2026, 8, 31))
        self.assertIsNone(market_session(date(2026, 9, 7)))
        self.assertEqual(next_market_session(date(2026, 9, 7)).session_date, date(2026, 9, 8))
        self.assertEqual(previous_market_session(date(2026, 9, 8)).session_date, date(2026, 9, 4))

    def test_early_close_moves_exit_and_label_capture(self) -> None:
        config = json.loads((ROOT / "config/runtime.json").read_text(encoding="utf-8"))
        schedule = session_times(date(2026, 11, 27), config)
        self.assertTrue(schedule["early_close"])
        self.assertEqual(schedule["market_close"].hour, 13)
        self.assertEqual(schedule["exit_at"].strftime("%H:%M"), "12:55")
        self.assertEqual(schedule["exit_deadline"].strftime("%H:%M"), "12:58")
        self.assertEqual(schedule["label_capture_after"].strftime("%H:%M:%S"), "13:00:05")

    def test_startup_failure_never_modifies_an_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            runtime_root = Path(name)
            previous = runtime_root / "SHAQ-CANARY-2026-08-25-001"
            previous.mkdir()
            report = previous / "professor_report.html"
            report.write_text("immutable-old-report", encoding="utf-8")
            observed = datetime(2026, 8, 29, 22, 30, tzinfo=ZoneInfo("America/New_York"))
            workflow = Workflow(
                package_root=ROOT, runtime_root=runtime_root, now=lambda: observed
            )
            failure_root = workflow.failure_record(ValueError("startup failed"))
            self.assertEqual(report.read_text(encoding="utf-8"), "immutable-old-report")
            self.assertEqual(failure_root.parent.name, "startup_failures")
            self.assertTrue((failure_root / "workflow_failure.json").is_file())

    def test_openai_request_has_no_tools_or_storage_and_never_records_the_key(self) -> None:
        captured = {}

        class Response:
            id = "resp-test"
            model = "gpt-test-snapshot"
            output_text = '{"results": []}'
            usage = None

        class Responses:
            def create(self, **kwargs):
                captured.update(kwargs)
                return Response()

        class Client:
            def __init__(self, **kwargs):
                captured["client"] = kwargs
                self.responses = Responses()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-test-key"}), patch(
            "openai.OpenAI", Client
        ):
            parsed, audit = _openai_call(
                prompt="frozen packet", schema={"type": "object"},
                config=self.ai_config(), workspace_root=ROOT.parent,
            )
            with tempfile.TemporaryDirectory() as name:
                path = Path(name) / "attestation.json"
                artifact = attest_openai_responses(
                    workspace_root=ROOT.parent, output=path, config=self.ai_config()
                )
                self.assertNotIn("secret-test-key", path.read_text(encoding="utf-8"))
                self.assertTrue(artifact["status"]["formal_ai_enabled"])
        self.assertEqual(parsed, {"results": []})
        self.assertEqual(captured["tools"], [])
        self.assertEqual(captured["tool_choice"], "none")
        self.assertFalse(captured["store"])
        self.assertTrue(captured["text"]["format"]["strict"])
        self.assertEqual(audit["backend"], "openai-responses-api")
        self.assertNotIn("api_key", audit)

    def test_openai_failure_does_not_fall_back_to_codex(self) -> None:
        class Responses:
            def create(self, **kwargs):
                raise RuntimeError("provider unavailable")

        class Client:
            def __init__(self, **kwargs):
                self.responses = Responses()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-test-key"}), patch(
            "openai.OpenAI", Client
        ), patch("shaq_daily_oracle.sandboxed_codex._codex_call") as codex:
            with self.assertRaises(SandboxedCodexError):
                _inference_call(
                    prompt="frozen packet", schema={"type": "object"},
                    config=self.ai_config(), workspace_root=ROOT.parent,
                )
            codex.assert_not_called()

    def test_live_ai_probe_requires_the_exact_ready_response(self) -> None:
        audit = {"model": "gpt-test", "prompt_sha256": "a", "output_sha256": "b"}
        with patch(
            "shaq_daily_oracle.sandboxed_codex._inference_call",
            return_value=({"status": "ready"}, audit),
        ):
            self.assertEqual(
                probe_ai_backend(
                    config=self.ai_config(), workspace_root=ROOT.parent,
                    timeout_seconds=60,
                ),
                audit,
            )
        with patch(
            "shaq_daily_oracle.sandboxed_codex._inference_call",
            return_value=({"status": "not-ready"}, audit),
        ):
            with self.assertRaises(SandboxedCodexError):
                probe_ai_backend(
                    config=self.ai_config(), workspace_root=ROOT.parent,
                    timeout_seconds=60,
                )

    def test_setup_keeps_key_out_of_files_and_writes_effective_backend(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            universe = Path(name) / "universe.csv"
            universe.write_text("symbol,gics_sector\nAAPL,Information Technology\n", encoding="utf-8")
            store = SettingsStore(paths)
            with patch.object(store, "set_openai_key") as save_key, patch.object(
                store, "get_openai_key", return_value="protected-key"
            ):
                saved = store.save_setup({
                    "ai_backend": "openai-responses", "model": "gpt-test",
                    "openai_api_key": "protected-key", "sec_identity": "Research contact@example.edu",
                    "opend_host": "127.0.0.1", "opend_port": 11111,
                    "universe_file": str(universe), "automatic_start_et": "08:15:00",
                })
            save_key.assert_called_once_with("protected-key")
            self.assertTrue(saved["setup_complete"])
            combined = paths.settings_file.read_text() + paths.effective_ai_config.read_text()
            self.assertNotIn("protected-key", combined)
            effective = json.loads(paths.effective_ai_config.read_text())
            self.assertEqual(effective["backend"], "openai-responses")
            self.assertEqual(effective["model"], "gpt-test")

    def test_codex_setup_needs_no_api_key_and_rebinds_backend_identity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            universe = Path(name) / "universe.csv"
            universe.write_text(
                "symbol,gics_sector\nAAPL,Information Technology\n", encoding="utf-8"
            )
            store = SettingsStore(paths)
            saved = store.save_setup({
                "ai_backend": "codex-cli", "model": "gpt-test",
                "sec_identity": "Research contact@example.edu",
                "opend_host": "127.0.0.1", "opend_port": 11111,
                "universe_file": str(universe), "automatic_start_et": "08:15:00",
            })
            self.assertTrue(saved["setup_complete"])
            effective = json.loads(paths.effective_ai_config.read_text())
            self.assertEqual(effective["backend"], "codex-cli")
            self.assertEqual(
                effective["parameter_bindings"]["backend"],
                ["REF-OPENAI-CODEX-AUTH-001", "DEC-AI-BACKEND-001", "EXP-DESKTOP-CODEX-001"],
            )

    def test_setup_is_not_complete_until_live_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            bridge = DesktopBridge.__new__(DesktopBridge)
            bridge.paths = paths
            bridge.store = SettingsStore(paths)
            submitted = {
                "setup_complete": True, "ai_backend": "openai-responses",
                "model": "gpt-test", "sec_identity": "Research contact@example.edu",
                "opend_host": "127.0.0.1", "opend_port": 11111,
                "universe_file": str(Path(name) / "universe.csv"),
                "automatic_start_et": "08:15:00",
            }
            with patch.object(bridge.store, "save_setup", return_value=submitted), patch.object(
                bridge, "_doctor_checks", return_value={
                    "ai_model_ready": True, "ai_isolation_ready": True,
                    "opend_reachable": True,
                    "simulate_account_ready": False, "universe_available": True,
                }
            ):
                result = bridge.save_setup({})
            self.assertFalse(result["ok"])
            self.assertFalse(json.loads(paths.settings_file.read_text())["setup_complete"])

    def test_weekend_worker_is_idle_and_creates_no_run(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            store = SettingsStore(paths)
            ready = {
                **store.load(), "setup_complete": True, "automatic_run_enabled": True,
            }
            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=None):
                self.assertEqual(run_worker(paths=paths, once=True), 0)
            status = json.loads((paths.runtime_root / "service_status.json").read_text())
            self.assertEqual(status["state"], "market_closed")
            self.assertFalse(any(paths.runtime_root.glob("SHAQ-CANARY-*-*")))

    def test_worker_does_not_backfill_after_evidence_cutoff(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 31, 10, 0, tzinfo=tz)

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            ready = {
                **SettingsStore(paths).load(), "setup_complete": True,
                "automatic_run_enabled": True,
            }
            session = SimpleNamespace(
                session_date=date(2026, 8, 31),
                market_close=FixedDateTime(2026, 8, 31, 16, 0, tzinfo=ZoneInfo("America/New_York")),
            )
            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=session), patch(
                "shaq_daily_oracle.service.datetime", FixedDateTime
            ):
                self.assertEqual(run_worker(paths=paths, once=True), 0)
            status = json.loads((paths.runtime_root / "service_status.json").read_text())
            self.assertEqual(status["state"], "missed_daily_cutoff")
            self.assertFalse(any(paths.runtime_root.glob("SHAQ-CANARY-*-*")))

    def test_corrected_engineering_failure_is_never_resumed(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 1, 11, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            runtime = paths.runtime_root / "SHAQ-CANARY-2026-09-01-001"
            runtime.mkdir()
            (runtime / "workflow_identity.json").write_text("{}", encoding="utf-8")
            (runtime / "correction_2026-09-01.json").write_text(json.dumps({
                "backfill_allowed": False,
                "excluded_from_professor_summary": True,
            }), encoding="utf-8")
            ready = {
                **SettingsStore(paths).load(), "setup_complete": True,
                "automatic_run_enabled": True,
            }
            session = SimpleNamespace(
                session_date=date(2026, 9, 1),
                market_close=FixedDateTime(
                    2026, 9, 1, 16, 0, tzinfo=ZoneInfo("America/New_York")
                ),
            )
            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=session), patch(
                "shaq_daily_oracle.service.datetime", FixedDateTime
            ), patch("shaq_daily_oracle.service.run_campaign") as campaign:
                result = run_worker(paths=paths, once=True)
            self.assertEqual(result, 0)
            campaign.assert_not_called()
            status = json.loads((paths.runtime_root / "service_status.json").read_text())
            self.assertEqual(status["state"], "missed_daily_cutoff")
            self.assertFalse(status["orders_submitted"])

    def test_single_worker_owns_the_due_postmortem(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 2, 16, 6, tzinfo=tz)

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            runtime = paths.runtime_root / "SHAQ-CANARY-2026-09-02-001"
            runtime.mkdir()
            (runtime / "audit_complete.json").write_text("{}", encoding="utf-8")
            ready = {
                **SettingsStore(paths).load(), "setup_complete": True,
                "automatic_run_enabled": True,
            }
            session = SimpleNamespace(
                session_date=date(2026, 9, 2),
                market_close=FixedDateTime(
                    2026, 9, 2, 16, 0, tzinfo=ZoneInfo("America/New_York")
                ),
            )
            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=session), patch(
                "shaq_daily_oracle.service.datetime", FixedDateTime
            ), patch("shaq_daily_oracle.service.PostmortemRunner") as runner:
                result = run_worker(paths=paths, once=True)
            self.assertEqual(result, 0)
            runner.return_value.run.assert_called_once_with(
                session_date=date(2026, 9, 2), phase="provisional"
            )

    def test_campaign_worker_starts_one_research_companion(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 31, 8, 16, tzinfo=tz)

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            (paths.data_root / "campaign.json").write_text(json.dumps({
                "campaign_id": "test", "session_dates": ["2026-08-31"],
                "runtime_root": str(paths.runtime_root), "workflow_start_et": "08:20:00",
                "heartbeat_seconds": 30, "active_health_seconds": 60,
                "idle_health_seconds": 300, "opend_host": "127.0.0.1", "opend_port": 11111,
            }), encoding="utf-8")
            ready = {
                **SettingsStore(paths).load(), "setup_complete": True,
                "automatic_run_enabled": True,
            }
            session = SimpleNamespace(
                session_date=date(2026, 8, 31),
                market_close=FixedDateTime(2026, 8, 31, 16, 0, tzinfo=ZoneInfo("America/New_York")),
            )
            companion_calls = []
            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=session), patch(
                "shaq_daily_oracle.service.datetime", FixedDateTime
            ), patch("shaq_daily_oracle.service.run_campaign", return_value=0):
                result = run_worker(
                    paths=paths, once=True,
                    research_companion=lambda: companion_calls.append("called"),
                )
            self.assertEqual(result, 0)
            self.assertEqual(companion_calls, ["called"])

    def test_shadow_failure_does_not_fail_the_formal_campaign(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 2, 8, 16, tzinfo=tz)

        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            (paths.data_root / "campaign.json").write_text(json.dumps({
                "campaign_id": "test", "session_dates": ["2026-09-02"],
                "runtime_root": str(paths.runtime_root), "workflow_start_et": "08:20:00",
                "heartbeat_seconds": 30, "active_health_seconds": 60,
                "idle_health_seconds": 300, "opend_host": "127.0.0.1", "opend_port": 11111,
            }), encoding="utf-8")
            ready = {
                **SettingsStore(paths).load(), "setup_complete": True,
                "automatic_run_enabled": True,
            }
            session = SimpleNamespace(
                session_date=date(2026, 9, 2),
                market_close=FixedDateTime(2026, 9, 2, 16, 0, tzinfo=ZoneInfo("America/New_York")),
            )

            def broken_shadow() -> None:
                raise RuntimeError("shadow failed")

            with patch.object(SettingsStore, "load", return_value=ready), patch.object(
                SettingsStore, "apply_to_environment", return_value={}
            ), patch("shaq_daily_oracle.service.market_session", return_value=session), patch(
                "shaq_daily_oracle.service.datetime", FixedDateTime
            ), patch("shaq_daily_oracle.service.run_campaign", return_value=0), patch(
                "shaq_daily_oracle.service._notify"
            ):
                result = run_worker(
                    paths=paths, once=True, research_companion=broken_shadow,
                )
            self.assertEqual(result, 0)
            status = json.loads((paths.runtime_root / "service_status.json").read_text())
            self.assertEqual(status["state"], "research_companion_failed")
            self.assertEqual(status["formal_prediction_effect"], "none")
            shadow_failure = json.loads(
                (paths.runtime_root / "shadow_failure_latest.json").read_text()
            )
            self.assertFalse(shadow_failure["orders_submitted"])

    def test_dashboard_is_disposable_and_rebuilds_from_frozen_sources(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            paths = self.paths(Path(name))
            runtime = paths.runtime_root / "SHAQ-CANARY-2026-08-31-001"
            runtime.mkdir()
            unsigned = {
                "run_id": runtime.name, "mode": "canary",
                "cutoff_et": "2026-08-31T08:50:00-04:00",
                "predictions": [{"symbol": "AAPL", "direction": "bullish", "track": "ordinary"}],
                "reports_by_symbol": {"AAPL": []}, "adversary_by_symbol": {},
                "integration_audit_by_symbol": {},
            }
            frozen = {**unsigned, "run_sha256": sha256_payload(unsigned)}
            (runtime / "frozen_run.json").write_text(json.dumps(frozen), encoding="utf-8")
            (runtime / "workflow_identity.json").write_text(json.dumps({
                "trade_date": "2026-08-31", "system_identity": "TEST-IDENTITY",
            }), encoding="utf-8")
            (runtime / "broker_journal.json").write_text(json.dumps({"orders": {}}), encoding="utf-8")
            (runtime / "execution_ledger.json").write_text(json.dumps({"round_trips": [{
                "symbol": "AAPL", "net_pnl": -2.0, "fees": 0.2,
                "outcome_status": "complete",
            }]}), encoding="utf-8")
            (runtime / "candidate_seed_intake.json").write_text(json.dumps({
                "candidates": [{
                    "symbol": "AAPL", "gics_sector": "Information Technology",
                }]
            }), encoding="utf-8")
            (runtime / "audit_complete.json").write_text("{}", encoding="utf-8")
            index = DashboardIndex(runtime_root=paths.runtime_root, database=paths.dashboard_db)
            first = index.overview()
            self.assertEqual(first["totals"]["predictions"], 1)
            self.assertEqual(first["performance"]["maximum_drawdown"], -2.0)
            self.assertEqual(first["performance"]["long_predictions"], 1)
            self.assertEqual(
                first["performance"]["sector_predictions"],
                {"Information Technology": 1},
            )
            exported = index.export_professor_report(Path(name) / "professor.html")
            public_text = exported.read_text(encoding="utf-8")
            self.assertNotIn(name, public_text)
            self.assertNotIn("account", public_text.lower())
            paths.dashboard_db.unlink()
            second = index.overview()
            self.assertEqual(first["runs"], second["runs"])
            (paths.runtime_root / "campaign_failure_latest.json").write_text(json.dumps({
                "recorded_at_et": "2026-08-31T07:45:00-04:00",
                "stage": "dynamic_health_checks", "error_type": "CampaignError",
                "message": "OpenD unavailable", "impact": {"orders_submitted": False},
            }), encoding="utf-8")
            incident = index.overview()["health"]["latest_incident"]
            self.assertEqual(incident["stage"], "dynamic_health_checks")
            self.assertFalse(incident["impact"]["orders_submitted"])
            (runtime / "correction_2026-08-30.json").write_text(json.dumps({
                "status": "audit_correction",
                "professor_summary_policy": "This run remains excluded from professor performance summaries.",
            }), encoding="utf-8")
            excluded = index.overview()
            self.assertEqual(excluded["totals"]["runs"], 0)
            self.assertTrue(excluded["runs"][0]["excluded"])
            frozen["predictions"][0]["direction"] = "bearish"
            (runtime / "frozen_run.json").write_text(json.dumps(frozen), encoding="utf-8")
            damaged = index.overview()
            self.assertFalse(damaged["latest"]["audit_valid"])
            self.assertEqual(damaged["latest"]["predictions"], [])


if __name__ == "__main__":
    unittest.main()
