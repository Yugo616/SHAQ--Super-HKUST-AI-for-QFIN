from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .deep_capture import capture_futu_deep_evidence
from .evidence_manifest import merge_evidence_manifests
from .events import capture_sec_candidate_events
from .hashing import sha256_file, sha256_payload
from .lineage import build_lineage_graph
from .reports import write_reports
from .run import freeze_run
from .schedule import formal_mode, session_times
from .tasks import build_blind_domain_tasks


class WorkflowError(RuntimeError):
    """The single-entry workflow failed closed."""


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any], *, immutable: bool = True) -> None:
    if immutable and path.exists():
        raise FileExistsError(f"immutable workflow artifact exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _run_id(runtime_root: Path, session_date: date) -> tuple[str, Path]:
    prefix = f"V6-CANARY-{session_date.isoformat()}-"
    existing = sorted(path for path in runtime_root.glob(prefix + "*") if path.is_dir())
    for path in reversed(existing):
        if not (path / "audit_complete.json").exists():
            return path.name, path
    sequence = max(
        (int(path.name.removeprefix(prefix)) for path in existing if path.name.removeprefix(prefix).isdigit()),
        default=0,
    ) + 1
    run_id = prefix + f"{sequence:03d}"
    return run_id, runtime_root / run_id


def _resolve_universe(runtime_root: Path) -> Path:
    configured = os.environ.get("DAILY_ORACLE_UNIVERSE")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise WorkflowError("DAILY_ORACLE_UNIVERSE does not name a file")
        return path
    candidates = sorted(
        runtime_root.glob("*/universe/effective_universe_formal.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise WorkflowError("no verified PIT universe is available; set DAILY_ORACLE_UNIVERSE")
    return candidates[0]


class Workflow:
    def __init__(
        self, *, package_root: Path, runtime_root: Path | None = None,
        host: str | None = None, port: int | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.package_root = package_root.resolve()
        self.runtime_root = (runtime_root or self.package_root / "runtime").resolve()
        self.host = host or os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
        self.port = port or int(os.environ.get("FUTU_OPEND_PORT", "11111"))
        self.runtime_config = _read(self.package_root / "config/runtime.json")
        self.zone = ZoneInfo(self.runtime_config["timezone"])
        self.now = now or (lambda: datetime.now(self.zone))
        self.sleep = sleep

    def _script(self, name: str, *arguments: str) -> None:
        result = subprocess.run(
            [sys.executable, str(self.package_root / "scripts" / name), *arguments],
            cwd=self.package_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise WorkflowError(f"{name} failed: {message[-2000:]}")

    def _wait(self, target: datetime) -> None:
        while True:
            seconds = (target - self.now()).total_seconds()
            if seconds <= 0:
                return
            self.sleep(min(seconds, 30.0))

    def _state(self, runtime: Path, stage: str, status: str, detail: str | None = None) -> None:
        path = runtime / "workflow_state.json"
        value = _read(path) if path.exists() else {"schema_version": 6, "stages": {}}
        value["stages"][stage] = {
            "status": status,
            "observed_at_et": self.now().isoformat(),
            "detail": detail,
        }
        _write(path, value, immutable=False)

    def _stage(self, runtime: Path, name: str, outputs: list[Path], action: Callable[[], None]) -> None:
        if outputs and all(path.exists() for path in outputs):
            self._state(runtime, name, "resumed")
            return
        if any(path.exists() for path in outputs):
            raise WorkflowError(f"partial immutable stage requires manual audit: {name}")
        self._state(runtime, name, "running")
        try:
            action()
        except Exception as exc:
            self._state(runtime, name, "failed", f"{type(exc).__name__}: {exc}")
            raise
        if any(not path.exists() for path in outputs):
            raise WorkflowError(f"stage did not create its declared artifacts: {name}")
        self._state(runtime, name, "complete")

    def run(self, *, requested_mode: str, session_date: date | None = None, wait: bool = True) -> Path:
        if requested_mode not in {"paper", "shadow"}:
            raise WorkflowError("mode must be paper or shadow")
        current = self.now()
        trade_date = session_date or current.date()
        schedule = session_times(trade_date, self.runtime_config)
        mode = "shadow" if requested_mode == "shadow" else formal_mode(current, schedule)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        run_id, runtime = _run_id(self.runtime_root, trade_date)
        runtime.mkdir(parents=True, exist_ok=True)
        evidence_root = runtime / "evidence"
        universe = _resolve_universe(self.runtime_root)
        initial = runtime / "workflow_identity.json"
        if not initial.exists():
            _write(initial, {
                "schema_version": 6,
                "run_id": run_id,
                "trade_date": trade_date.isoformat(),
                "requested_mode": requested_mode,
                "effective_mode_at_start": mode,
                "started_at_et": current.isoformat(),
                "universe_sha256": sha256_file(universe),
                "schedule": {key: value.isoformat() for key, value in schedule.items()},
            })
        elif _read(initial).get("universe_sha256") != sha256_file(universe):
            raise WorkflowError("resumed run resolved a different universe")
        if (runtime / "audit_complete.json").exists():
            return runtime
        universe_manifest = universe.parent / "active_manifest.json"
        if universe_manifest.is_file() and not (runtime / "universe/active_manifest.json").exists():
            (runtime / "universe").mkdir(parents=True, exist_ok=True)
            shutil.copy2(universe_manifest, runtime / "universe/active_manifest.json")

        tests = runtime / "tests_report.json"
        self._stage(runtime, "release_preflight", [tests], lambda: self._script(
            "run_acceptance_tests.py", "--output", str(tests)
        ))
        if not _read(tests).get("public_passed") or not _read(tests).get("release_validator_passed"):
            mode = "shadow"
        if wait and self.now() < schedule["precheck_start"]:
            self._wait(schedule["precheck_start"])
        if self.now() > schedule["evidence_cutoff"]:
            mode = "shadow"

        stocks = runtime / "stock_snapshot.json"
        benchmarks = runtime / "benchmark_snapshot.json"
        benchmark_csv = self.package_root / "config/market-benchmarks.csv"
        capture_cutoff = schedule["evidence_cutoff"]
        if mode == "shadow" and self.now() > capture_cutoff:
            capture_cutoff = self.now() + timedelta(minutes=20)
        cutoff_text = capture_cutoff.isoformat()
        self._stage(runtime, "premarket_snapshot", [stocks, benchmarks], lambda: (
            self._script(
                "capture_futu_premarket.py", "--universe", str(universe),
                "--cutoff-et", cutoff_text, "--output", str(stocks),
                "--split-dir", str(evidence_root / "stocks_by_symbol"),
                "--host", self.host, "--port", str(self.port),
            ),
            self._script(
                "capture_futu_premarket.py", "--universe", str(benchmark_csv),
                "--cutoff-et", cutoff_text, "--output", str(benchmarks),
                "--split-dir", str(evidence_root / "benchmarks_by_symbol"),
                "--host", self.host, "--port", str(self.port),
            ),
        ))

        seed_intake = runtime / "candidate_seed_intake.json"
        candidate_policy = runtime / "candidate_policy.json"
        self._stage(runtime, "candidate_intake", [seed_intake, candidate_policy], lambda: self._script(
            "select_candidates.py", "--stocks", str(stocks), "--benchmarks", str(benchmarks),
            "--universe", str(universe), "--policy-snapshot", str(candidate_policy),
            "--output", str(seed_intake),
        ))
        seed_candidates = _read(seed_intake).get("candidates", [])

        history_manifest = runtime / "price_history_manifest.json"
        history_dir = evidence_root / "price_history_by_symbol"
        self._stage(runtime, "price_history", [history_manifest, history_dir], lambda: self._script(
            "capture_futu_price_history.py", "--candidates", str(seed_intake),
            "--stock-snapshot", str(stocks), "--benchmark-snapshot", str(benchmarks),
            "--evidence-root", str(evidence_root), "--end-date", _previous_weekday(trade_date).isoformat(),
            "--cutoff-et", cutoff_text, "--split-dir", str(history_dir),
            "--output", str(history_manifest), "--host", self.host, "--port", str(self.port),
        ))

        event_manifest = runtime / "event_evidence_manifest.json"
        event_status = runtime / "event_collection_status.json"
        previous_close = datetime.combine(
            _previous_weekday(trade_date), datetime.min.time().replace(hour=16), self.zone
        )
        self._stage(runtime, "primary_events", [event_manifest, event_status], lambda: capture_sec_candidate_events(
            candidates=seed_candidates, universe_csv=universe, evidence_root=evidence_root,
            output_manifest=event_manifest, status_output=event_status, cutoff_et=cutoff_text,
            previous_close_et=previous_close.isoformat(),
        ))

        intake = runtime / "candidate_intake.json"
        def finalize_intake() -> None:
            document = _read(seed_intake)
            event_symbols = {
                scope
                for record in _read(event_manifest).get("evidence", [])
                for scope in record.get("scope_symbols", [])
            }
            for row in document.get("candidates", []):
                if row["symbol"] in event_symbols:
                    row["captured_primary_event"] = True
            document["event_candidate_count"] = len(event_symbols)
            _write(intake, document)
        self._stage(runtime, "candidate_event_binding", [intake], finalize_intake)
        candidates = _read(intake).get("candidates", [])

        snapshot_manifest = runtime / "snapshot_evidence_manifest.json"
        self._stage(runtime, "snapshot_manifest", [snapshot_manifest], lambda: self._script(
            "build_snapshot_evidence_manifest.py", "--stocks", str(stocks),
            "--benchmarks", str(benchmarks), "--evidence-root", str(evidence_root),
            "--output", str(snapshot_manifest),
        ))

        deep_manifest = runtime / "deep_evidence_manifest.json"
        collection_status = runtime / "collection_status.json"
        deep_config = _read(self.package_root / "config/deep-evidence.json")
        self._stage(runtime, "deep_evidence", [deep_manifest, collection_status], lambda: capture_futu_deep_evidence(
            candidates=candidates, universe_csv=universe, price_history_dir=history_dir,
            evidence_root=evidence_root, output_manifest=deep_manifest,
            status_output=collection_status, cutoff_et=cutoff_text, config=deep_config,
            host=self.host, port=self.port,
        ))

        evidence_manifest = runtime / "evidence_manifest.json"
        def merge() -> None:
            base = _read(snapshot_manifest)
            additions = (
                _read(history_manifest).get("evidence", [])
                + _read(deep_manifest).get("evidence", [])
                + _read(event_manifest).get("evidence", [])
            )
            _write(evidence_manifest, merge_evidence_manifests(base, additions))
        self._stage(runtime, "evidence_merge", [evidence_manifest], merge)

        lineage_path = runtime / "lineage.json"
        analysis_cutoff = cutoff_text
        self._stage(runtime, "lineage", [lineage_path], lambda: _write(
            lineage_path,
            build_lineage_graph(_read(evidence_manifest)["evidence"], evidence_root, analysis_cutoff),
        ))
        tasks_path = runtime / "domain_tasks.json"
        self._stage(runtime, "blind_tasks", [tasks_path], lambda: _write(
            tasks_path,
            build_blind_domain_tasks(
                lineage=_read(lineage_path), symbols=[row["symbol"] for row in candidates],
                as_of_et=analysis_cutoff,
                horizon="official_US_regular_session_open_to_close",
            ),
        ))

        isolation_status = runtime / "isolation_status.json"
        attestation = runtime / "isolation_attestation.json"
        def isolate() -> None:
            if os.environ.get("DAILY_ORACLE_DISABLE_AI") == "1":
                self._script(
                    "snapshot_isolation.py", "--output", str(isolation_status), "--backend", "disabled"
                )
                return
            try:
                self._script(
                    "snapshot_isolation.py", "--output", str(isolation_status),
                    "--backend", "sandboxed-codex", "--workspace-root", str(self.package_root.parent),
                    "--attestation", str(attestation),
                )
            except Exception:
                if attestation.exists():
                    attestation.rename(runtime / "isolation_attestation.failed.json")
                self._script("snapshot_isolation.py", "--output", str(isolation_status), "--backend", "disabled")
        self._stage(runtime, "isolation", [isolation_status], isolate)
        isolation = _read(isolation_status)

        run_input = runtime / "run_input.json"
        if not run_input.exists():
            if candidates and isolation.get("formal_ai_enabled") is True:
                try:
                    self._script(
                        "run_sandboxed_six_domain.py", "--run-id", run_id,
                        "--cutoff-et", analysis_cutoff, "--tasks", str(tasks_path),
                        "--lineage", str(lineage_path), "--candidate-intake", str(intake),
                        "--evidence-manifest", str(evidence_manifest), "--evidence-root", str(evidence_root),
                        "--isolation-status", str(isolation_status), "--attestation", str(attestation),
                        "--calls-dir", str(runtime / "ai_calls"), "--output", str(run_input),
                    )
                except Exception as exc:
                    mode = "shadow"
                    _write(runtime / "ai_failure.json", {
                        "schema_version": 6,
                        "failed_at_et": self.now().isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "fallback": "no_ai_shadow",
                    })
                    fallback_status = runtime / "isolation_status_fallback.json"
                    self._script(
                        "snapshot_isolation.py", "--output", str(fallback_status), "--backend", "disabled"
                    )
                    isolation_status = fallback_status
                    isolation = _read(fallback_status)
                    self._script(
                        "build_no_ai_run_input.py", "--run-id", run_id,
                        "--cutoff-et", analysis_cutoff, "--candidate-intake", str(intake),
                        "--evidence-manifest", str(evidence_manifest),
                        "--isolation-status", str(isolation_status), "--output", str(run_input),
                    )
            else:
                self._script(
                    "build_no_ai_run_input.py", "--run-id", run_id,
                    "--cutoff-et", analysis_cutoff, "--candidate-intake", str(intake),
                    "--evidence-manifest", str(evidence_manifest),
                    "--isolation-status", str(isolation_status), "--output", str(run_input),
                )
            value = _read(run_input)
            value["publication_deadline_et"] = schedule["forecast_deadline"].isoformat()
            value["formal_eligibility"] = mode == "paper"
            _write(run_input, value, immutable=False)
        self._state(runtime, "six_domain_and_adversary", "complete" if isolation.get("formal_ai_enabled") else "no_ai")

        frozen_path = runtime / "frozen_run.json"
        integration_snapshot = runtime / "integration_policy.json"
        integration_policy = _read(self.package_root / "config/integration.json")
        def freeze() -> None:
            created = self.now()
            value = _read(run_input)
            value["created_at"] = created.isoformat()
            value["formal_eligibility"] = mode == "paper"
            frozen = freeze_run(
                run_input=value, evidence_root=evidence_root,
                integration_policy=integration_policy,
                candidate_intake_sha256=sha256_file(intake),
                isolation_status=isolation,
            )
            _write(integration_snapshot, integration_policy)
            _write(frozen_path, frozen)
        self._stage(runtime, "forecast_freeze", [frozen_path, integration_snapshot], freeze)

        portfolio = runtime / "portfolio_snapshot.json"
        self._stage(runtime, "portfolio_snapshot", [portfolio], lambda: self._script(
            "snapshot_futu_portfolio.py", "--output", str(portfolio),
            "--account-alias", str(self.runtime_config["account_alias"]),
            "--host", self.host, "--port", str(self.port),
        ))
        borrow = runtime / "borrowability.json"
        symbols = [row["symbol"] for row in _read(frozen_path).get("predictions", [])]
        self._stage(runtime, "borrowability", [borrow], lambda: self._script(
            "snapshot_futu_borrowability.py", *symbols, "--output", str(borrow),
            "--host", self.host, "--port", str(self.port),
        ))

        dynamic_policy = runtime / "execution_policy.template.json"
        if not dynamic_policy.exists():
            template = _read(self.package_root / "config/canary.example.json")
            template.update({
                "run_id": run_id,
                "forecast_cutoff": schedule["forecast_deadline"].isoformat(),
                "entry_after": schedule["entry_after"].isoformat(),
                "entry_deadline": schedule["entry_deadline"].isoformat(),
                "exit_at": schedule["exit_at"].isoformat(),
                "exit_deadline": schedule["exit_deadline"].isoformat(),
                "shares_per_forecast": int(self.runtime_config["shares_per_forecast"]),
                "max_forecasts": int(self.runtime_config["maximum_forecasts"]),
                "account_allowlist": [self.runtime_config["account_alias"]],
            })
            _write(dynamic_policy, template)
        intents = runtime / "order_intents.json"
        policy_snapshot = runtime / "execution_policy.json"
        self._stage(runtime, "order_intents", [intents, policy_snapshot], lambda: self._script(
            "build_canary.py", "--forecast", str(frozen_path), "--portfolio", str(portfolio),
            "--borrow", str(borrow), "--policy", str(dynamic_policy),
            "--policy-snapshot", str(policy_snapshot), "--output", str(intents),
        ))
        label_placeholder = runtime / "label_placeholder.json"
        self._stage(runtime, "label_placeholder", [label_placeholder], lambda: self._script(
            "create_label_placeholder.py", "--forecast", str(frozen_path), "--output", str(label_placeholder),
        ))
        provisional_view = runtime / "labels_provisional.json"
        if not provisional_view.exists():
            _write(provisional_view, _read(label_placeholder))
        ledger_view = runtime / "execution_ledger.json"
        if not ledger_view.exists():
            _write(ledger_view, {
                "schema_version": 6,
                "run_id": run_id,
                "trd_env": "SIMULATE",
                "scientific_labels_are_separate": True,
                "status": "awaiting_execution",
                "round_trips": [],
            })
        write_reports(
            runtime=runtime, frozen=_read(frozen_path), collection_statuses={
                "statuses": _read(collection_status).get("statuses", []) + _read(event_status).get("statuses", [])
            },
            orders=_read(intents), labels=_read(label_placeholder),
        )

        if requested_mode == "paper" and _read(intents).get("mode") == "canary":
            journal = runtime / "broker_journal.json"
            if wait:
                entry_marker = runtime / "entry_complete.json"
                if not entry_marker.exists():
                    self._wait(schedule["entry_after"])
                    self._script(
                        "execute_futu_canary.py", "--intents", str(intents), "--frozen-run", str(frozen_path),
                        "--policy-snapshot", str(policy_snapshot), "--journal", str(journal),
                        "--phase", "entry", "--submit", "--host", self.host, "--port", str(self.port),
                    )
                    _write(entry_marker, {"completed_at_et": self.now().isoformat(), "journal_sha256": sha256_file(journal)})
                exit_marker = runtime / "exit_complete.json"
                if not exit_marker.exists():
                    self._wait(schedule["exit_at"])
                    self._script(
                        "execute_futu_canary.py", "--intents", str(intents), "--frozen-run", str(frozen_path),
                        "--policy-snapshot", str(policy_snapshot), "--journal", str(journal),
                        "--phase", "exit", "--submit", "--host", self.host, "--port", str(self.port),
                    )
                    _write(exit_marker, {"completed_at_et": self.now().isoformat(), "journal_sha256": sha256_file(journal)})
                reconcile_marker = runtime / "reconcile_complete.json"
                if not reconcile_marker.exists():
                    self._script(
                        "execute_futu_canary.py", "--intents", str(intents), "--frozen-run", str(frozen_path),
                        "--policy-snapshot", str(policy_snapshot), "--journal", str(journal),
                        "--phase", "reconcile", "--submit", "--host", self.host, "--port", str(self.port),
                    )
                    _write(reconcile_marker, {"completed_at_et": self.now().isoformat(), "journal_sha256": sha256_file(journal)})
                post_exit = runtime / "portfolio_post_exit.json"
                self._stage(runtime, "post_exit_portfolio", [post_exit], lambda: self._script(
                    "snapshot_futu_portfolio.py", "--output", str(post_exit),
                    "--account-alias", str(self.runtime_config["account_alias"]),
                    "--host", self.host, "--port", str(self.port),
                ))
                ledger_observed = runtime / "execution_ledger_observed.json"
                self._stage(runtime, "execution_ledger", [ledger_observed], lambda: self._script(
                    "build_execution_ledger.py", "--intents", str(intents),
                    "--journal", str(journal), "--output", str(ledger_observed),
                ))
                _write(ledger_view, _read(ledger_observed), immutable=False)
                self._wait(schedule["label_capture_after"])
                provisional_observed = runtime / "labels_observed_provisional.json"
                self._stage(runtime, "provisional_labels", [provisional_observed], lambda: self._script(
                    "capture_futu_labels.py", "--forecast", str(frozen_path),
                    "--trade-date", trade_date.isoformat(), "--phase", "provisional",
                    "--session-close-et", datetime.combine(
                        trade_date, datetime.min.time().replace(hour=16), self.zone
                    ).isoformat(),
                    "--output", str(provisional_observed), "--host", self.host, "--port", str(self.port),
                ))
                _write(provisional_view, _read(provisional_observed), immutable=False)
                evaluations = runtime / "evaluations_provisional.json"
                self._stage(runtime, "prospective_evaluations", [evaluations], lambda: self._script(
                    "build_prospective_evaluations.py", "--forecast", str(frozen_path),
                    "--labels", str(provisional_view), "--output", str(evaluations),
                ))
                readiness = runtime / "readiness_status.json"
                self._stage(runtime, "readiness", [readiness], lambda: self._script(
                    "evaluate_readiness.py", "--evaluations", str(evaluations),
                    "--round-trips", str(ledger_view), "--output", str(readiness),
                ))
                complete = runtime / "audit_complete.json"
                self._stage(runtime, "complete_audit", [complete], lambda: self._script(
                    "audit_canary.py", "--runtime", str(runtime), "--stage", "complete",
                    "--output", str(complete),
                ))
                write_reports(
                    runtime=runtime, frozen=_read(frozen_path), collection_statuses={
                        "statuses": _read(collection_status).get("statuses", []) + _read(event_status).get("statuses", [])
                    }, orders=_read(journal), labels=_read(provisional_view),
                )
            elif not journal.exists():
                _write(journal, {"schema_version": 6, "run_status": "NOT_SUBMITTED_NO_WAIT", "orders": {}})
        elif not (runtime / "broker_journal.json").exists():
            _write(runtime / "broker_journal.json", {
                "schema_version": 6, "run_status": "SHADOW_OR_EMPTY", "orders": {}
            })

        if not wait:
            _write(runtime / "workflow_validation.json", {
                "schema_version": 6,
                "run_id": run_id,
                "status": "workflow_validated_without_clock_wait_or_order_submission",
                "frozen_run_sha256": _read(frozen_path)["run_sha256"],
            }, immutable=not (runtime / "workflow_validation.json").exists())
        return runtime

    def failure_record(self, error: Exception) -> Path:
        current = self.now()
        candidates = sorted(self.runtime_root.glob("V6-CANARY-*"), key=lambda path: path.stat().st_mtime)
        runtime = candidates[-1] if candidates else self.runtime_root / f"V6-CANARY-{current.date().isoformat()}-FAILED"
        runtime.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 6,
            "status": "fail_closed",
            "recorded_at_et": current.isoformat(),
            "error_type": type(error).__name__,
            "message": str(error),
            "orders_submitted": False,
        }
        _write(runtime / "workflow_failure.json", payload, immutable=not (runtime / "workflow_failure.json").exists())
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Daily Oracle V6 fail-closed report</title></head>"
            f"<body><h1>Daily Oracle V6 — fail closed</h1><p>{type(error).__name__}</p>"
            "<p>No forecast was promoted and no order was submitted.</p></body></html>"
        )
        (runtime / "professor_report.html").write_text(body, encoding="utf-8")
        return runtime
