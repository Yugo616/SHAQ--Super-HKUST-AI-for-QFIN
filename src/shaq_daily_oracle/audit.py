from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload
from .execution import verify_execution_bundle
from .isolation import IsolationError, validate_isolation_status
from .sandboxed_codex import derive_predictions, verify_isolation_attestation
from .candidates import CandidateError, select_candidates
from .labels import LABEL_DOCUMENT_KEYS, LabelError, validate_label_document
from .readiness import ReadinessError, build_prospective_evaluations
from .sec_view import SecViewError, build_sec_analysis_text


class AuditError(ValueError):
    """A canary artifact set is incomplete or inconsistent."""


def _read(runtime: Path, name: str) -> dict[str, Any]:
    path = runtime / name
    if not path.is_file():
        raise AuditError(f"missing artifact: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_universe(runtime: Path, prediction_symbols: set[str]) -> dict[str, Any]:
    manifest = _read(runtime, "universe/active_manifest.json")
    pairs = (
        ("base_path", "base_sha256"),
        ("source_path", "source_sha256"),
        ("source_receipt_path", "source_receipt_file_sha256"),
        ("events_path", "events_sha256"),
        ("output_path", "output_sha256"),
    )
    resolved: dict[str, Path] = {}
    for path_key, hash_key in pairs:
        path = Path(str(manifest.get(path_key, "")))
        if not path.is_file() or sha256_file(path) != manifest.get(hash_key):
            raise AuditError(f"universe {path_key} is missing or its SHA-256 changed")
        resolved[path_key] = path
    receipt = json.loads(resolved["source_receipt_path"].read_text(encoding="utf-8"))
    receipt_hash = receipt.get("receipt_sha256")
    receipt_unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt_hash != sha256_payload(receipt_unsigned):
        raise AuditError("universe source receipt hash mismatch")
    if receipt.get("raw_sha256") != manifest.get("source_sha256"):
        raise AuditError("universe source receipt points to different bytes")
    with resolved["output_path"].open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    symbols = [str(row.get("instrument", "")).strip().upper() for row in rows]
    if not symbols or "" in symbols or len(symbols) != len(set(symbols)):
        raise AuditError("effective universe has blank or duplicate instruments")
    if len(symbols) != int(manifest.get("effective_count", -1)):
        raise AuditError("effective universe count differs from its manifest")
    if not prediction_symbols.issubset(set(symbols)):
        raise AuditError("a prediction is outside the effective universe")
    return {
        "effective_count": len(symbols),
        "output_sha256": manifest["output_sha256"],
        "derivation_sha256": manifest.get("derivation_sha256"),
    }


def _verify_candidate_intake(runtime: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    path = runtime / "candidate_intake.json"
    if not path.is_file() or sha256_file(path) != frozen.get("candidate_intake_sha256"):
        raise AuditError("candidate intake is missing or differs from the frozen run")
    intake = json.loads(path.read_text(encoding="utf-8"))
    policy = _read(runtime, "candidate_policy.json")
    inputs = intake.get("inputs", {})
    if inputs.get("candidate_policy_sha256") != sha256_payload(policy):
        raise AuditError("candidate intake policy hash mismatch")
    sources: dict[str, Path] = {}
    for prefix in ("stock_snapshot", "benchmark_snapshot", "universe", "benchmark_config"):
        source = Path(str(inputs.get(f"{prefix}_path", "")))
        if not source.is_file() or sha256_file(source) != inputs.get(f"{prefix}_sha256"):
            raise AuditError(f"candidate intake {prefix} changed")
        sources[prefix] = source
    serialized = json.dumps(intake, sort_keys=True).lower()
    if any(token in serialized for token in ('"direction"', '"score"', '"ranking"')):
        raise AuditError("candidate intake contains a proposed answer or ranking")
    candidates = intake.get("candidates", [])
    symbols = [str(row.get("symbol", "")).strip().upper() for row in candidates]
    if "" in symbols or len(symbols) != len(set(symbols)):
        raise AuditError("candidate intake has blank or duplicate symbols")
    report_symbols = set(frozen.get("reports_by_symbol", {}))
    if report_symbols != set(symbols):
        raise AuditError("six-domain report symbols differ from candidate intake")
    event_scopes = {
        str(symbol).upper()
        for record in frozen.get("lineage", {}).get("records", [])
        if record.get("domain") == "event"
        for symbol in record.get("scope_symbols", [])
    }
    if any(
        row.get("captured_primary_event") is True and row["symbol"] not in event_scopes
        for row in candidates
    ):
        raise AuditError("candidate intake claims an event without frozen event evidence")
    try:
        recomputed = select_candidates(
            stock_snapshot=json.loads(sources["stock_snapshot"].read_text(encoding="utf-8")),
            benchmark_snapshot=json.loads(sources["benchmark_snapshot"].read_text(encoding="utf-8")),
            universe_csv=sources["universe"],
            benchmark_csv=sources["benchmark_config"],
            captured_event_symbols=[
                row["symbol"] for row in candidates if row.get("captured_primary_event") is True
            ],
            excluded_symbols=list(intake.get("excluded_symbols", [])),
            policy=policy,
        )
    except (CandidateError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditError(f"candidate intake cannot be reproduced: {exc}") from exc
    observed = {key: value for key, value in intake.items() if key != "inputs"}
    if sha256_payload(recomputed) != sha256_payload(observed):
        raise AuditError("candidate intake differs from deterministic recomputation")
    return {"candidate_count": len(symbols), "candidate_intake_sha256": frozen["candidate_intake_sha256"]}


def _verify_ai_calls(runtime: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    calls = runtime / "ai_calls"
    names = ("market", "relationships", "event", "capital", "derivatives", "price_volume", "adversary")
    hashes = {}
    reconstructed_reports: dict[str, list[dict[str, Any]]] = {}
    reconstructed_adversary: dict[str, dict[str, Any]] = {}
    for name in names:
        artifact_path = calls / f"{name}.json"
        if not artifact_path.is_file():
            raise AuditError(f"missing formal AI call artifact: {name}")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if artifact.get("backend_config_sha256") != sha256_payload(artifact.get("backend_config")):
            raise AuditError(f"formal AI backend config hash mismatch: {name}")
        prompt_path = calls / str(artifact.get("prompt_file", ""))
        packet_path = calls / str(artifact.get("packet_file", ""))
        if (
            prompt_path.resolve().parent != calls.resolve()
            or packet_path.resolve().parent != calls.resolve()
        ):
            raise AuditError(f"formal AI artifact path escapes its call directory: {name}")
        if (
            not prompt_path.is_file()
            or not packet_path.is_file()
            or sha256_file(prompt_path) != artifact.get("prompt_file_sha256")
            or sha256_file(packet_path) != artifact.get("packet_file_sha256")
        ):
            raise AuditError(f"formal AI prompt or packet changed: {name}")
        if hashlib.sha256(prompt_path.read_bytes()).hexdigest() != artifact.get("audit", {}).get("prompt_sha256"):
            raise AuditError(f"formal AI prompt hash differs from inference audit: {name}")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        if sha256_payload(packet) != artifact.get("input_sha256"):
            raise AuditError(f"formal AI packet semantic hash mismatch: {name}")
        if sha256_payload(artifact.get("raw_output")) != artifact.get("audit", {}).get("output_sha256"):
            raise AuditError(f"formal AI result differs from inference audit: {name}")
        if name == "adversary":
            reconstructed_adversary = {
                row["symbol"]: row["report"] for row in artifact.get("results", [])
            }
        else:
            task_symbols = {row["task_id"]: row["symbol"] for row in packet.get("tasks", [])}
            for row in artifact.get("results", []):
                symbol = task_symbols.get(row.get("task_id"))
                if not symbol:
                    raise AuditError(f"formal AI result has no matching blind task: {name}")
                reconstructed_reports.setdefault(symbol, []).append(row["report"])
        hashes[name] = sha256_file(artifact_path)
    normalized_reports = {
        symbol: sorted(reports, key=lambda row: row["domain"])
        for symbol, reports in reconstructed_reports.items()
    }
    if normalized_reports != frozen.get("reports_by_symbol", {}):
        raise AuditError("frozen domain reports differ from first-valid isolated outputs")
    if reconstructed_adversary != frozen.get("adversary_by_symbol", {}):
        raise AuditError("frozen adversary reports differ from first-valid isolated outputs")
    return hashes


def audit_runtime(runtime: str | Path, stage: str) -> dict[str, Any]:
    if stage not in {"preflight", "complete"}:
        raise AuditError("stage must be preflight or complete")
    root = Path(runtime)
    frozen = _read(root, "frozen_run.json")
    declared_hash = frozen.get("run_sha256")
    unsigned = dict(frozen)
    unsigned.pop("run_sha256", None)
    if declared_hash != sha256_payload(unsigned):
        raise AuditError("frozen run hash mismatch")
    integration_policy = _read(root, "integration_policy.json")
    if frozen.get("integration_policy_sha256") != sha256_payload(integration_policy):
        raise AuditError("frozen run and integration policy differ")
    candidate_intake = _verify_candidate_intake(root, frozen)
    try:
        isolation = validate_isolation_status(_read(root, "isolation_status.json"))
    except IsolationError as exc:
        raise AuditError(f"invalid isolation status: {exc}") from exc
    if frozen.get("isolation_status_sha256") != sha256_payload(isolation):
        raise AuditError("frozen run and isolation status differ")
    if isolation.get("formal_ai_enabled") is not True and frozen.get("predictions"):
        raise AuditError("formal AI was disabled but the run contains predictions")
    if frozen.get("formal_ai_enabled") != isolation.get("formal_ai_enabled"):
        raise AuditError("frozen formal-AI flag differs from isolation status")
    if isolation.get("formal_ai_enabled") is True:
        try:
            verify_isolation_attestation(
                status=isolation,
                attestation_path=root / "isolation_attestation.json",
                workspace_root=Path(__file__).resolve().parents[3],
            )
        except IsolationError as exc:
            raise AuditError(f"formal AI attestation failed: {exc}") from exc
        ai_call_hashes = _verify_ai_calls(root, frozen)
        expected_predictions = derive_predictions(
            reports_by_symbol=frozen.get("reports_by_symbol", {}),
            adversary_by_symbol=frozen.get("adversary_by_symbol", {}),
            candidate_intake=_read(root, "candidate_intake.json"),
            integration_policy=integration_policy,
        )
        public_keys = ("symbol", "direction", "track", "industry_group")
        if [
            {key: row[key] for key in public_keys} for row in expected_predictions
        ] != [
            {key: row[key] for key in public_keys} for row in frozen.get("predictions", [])
        ]:
            raise AuditError("frozen predictions differ from deterministic lineage integration")
    else:
        ai_call_hashes = {}
    for record in frozen.get("lineage", {}).get("records", []):
        raw_path = Path(str(record.get("raw_file_path", "")))
        if not raw_path.is_file() or sha256_file(raw_path) != record.get("raw_sha256"):
            raise AuditError("frozen raw evidence is missing or its SHA-256 changed")
        if record.get("analysis_file_path"):
            analysis_path = Path(str(record["analysis_file_path"]))
            if not analysis_path.is_file() or sha256_file(analysis_path) != record.get("analysis_sha256"):
                raise AuditError("frozen analysis view is missing or its SHA-256 changed")
            transform = record.get("analysis_transform", {})
            try:
                rebuilt = build_sec_analysis_text(
                    raw_path.read_bytes(),
                    document_types=transform.get("document_types", []),
                    maximum_output_bytes=int(transform.get("maximum_output_bytes", 0)),
                )
            except (SecViewError, TypeError, ValueError) as exc:
                raise AuditError("frozen analysis view cannot be reconstructed") from exc
            if analysis_path.read_bytes() != rebuilt:
                raise AuditError("frozen analysis view differs from its raw source")
    predictions = frozen.get("predictions", [])
    if len(predictions) > 3:
        raise AuditError("forecast cap exceeded")
    for prediction in predictions:
        symbol = prediction["symbol"]
        reports = frozen.get("reports_by_symbol", {}).get(symbol, [])
        if len(reports) != 6 or len({report["domain"] for report in reports}) != 6:
            raise AuditError(f"{symbol} lacks six distinct reports")
        adversary = frozen.get("adversary_by_symbol", {}).get(symbol)
        if not adversary or adversary.get("counts_as_vote") is not False:
            raise AuditError(f"{symbol} lacks a non-voting adversary")
        if prediction.get("p_committee_hit") is not None or prediction.get("p_net_profit") is not None:
            raise AuditError("unapproved probability entered a forecast")
    universe = _verify_universe(root, {row["symbol"] for row in predictions})

    portfolio = _read(root, "portfolio_snapshot.json")
    borrowability = _read(root, "borrowability.json")
    intents = _read(root, "order_intents.json")
    execution_policy = _read(root, "execution_policy.json")
    verify_execution_bundle(intents, frozen, execution_policy)
    if (
        intents.get("portfolio_snapshot_sha256")
        != sha256_file(root / "portfolio_snapshot.json")
        or intents.get("borrowability_snapshot_sha256")
        != sha256_file(root / "borrowability.json")
    ):
        raise AuditError("intent bundle is not bound to its portfolio and borrow snapshots")
    placeholders = _read(root, "label_placeholder.json")
    tests = _read(root, "tests_report.json")
    if (
        portfolio.get("trd_env") != "SIMULATE"
        or borrowability.get("trd_env") != "SIMULATE"
        or intents.get("trd_env") != "SIMULATE"
    ):
        raise AuditError("runtime is not SIMULATE-only")
    if (
        tests.get("public_passed") is not True
        or tests.get("release_validator_passed") is not True
        or tests.get("legacy_passed") is False
        or tests.get("plugin_validator_passed") is False
    ):
        raise AuditError("test report is not green")
    if len(placeholders.get("labels", [])) != len(predictions):
        raise AuditError("label placeholder count differs from forecast count")
    external = set(intents.get("external_positions", []))
    if any(intent["symbol"] in external for intent in intents.get("intents", [])):
        raise AuditError("an external position entered SHAQ intents")
    configured_quantity = int(execution_policy.get("shares_per_forecast", 0))
    if configured_quantity <= 0 or any(
        intent.get("quantity") != configured_quantity for intent in intents.get("intents", [])
    ):
        raise AuditError("canary intent exceeds its configured share quantity")

    checked = [
        "frozen_hash", "candidate_intake_binding", "integration_policy_binding", "effective_universe_lineage", "six_domain_reports", "non_voting_adversary",
        "probability_null", "raw_evidence_rehash", "formal_ai_isolation", "simulate_only", "broker_snapshot_binding", "external_position_isolation",
        "configured_share_cap", "label_placeholders", "public_and_legacy_tests",
    ]
    if stage == "complete":
        journal = _read(root, "broker_journal.json")
        ledger = _read(root, "execution_ledger.json")
        post_portfolio = _read(root, "portfolio_post_exit.json")
        if post_portfolio.get("trd_env") != "SIMULATE":
            raise AuditError("post-exit portfolio is not SIMULATE")
        post_symbols = {
            row.get("symbol")
            for row in post_portfolio.get("positions", [])
            if float(row.get("quantity", 0)) != 0
        }
        intended_symbols = {row["symbol"] for row in intents.get("intents", [])}
        if post_symbols & intended_symbols:
            raise AuditError("a SHAQ intent symbol remains in the post-exit portfolio")
        if len(ledger.get("round_trips", [])) != len(intents.get("intents", [])):
            raise AuditError("execution ledger and intent counts differ")
        if any(
            row.get("outcome_status") not in {"round_trip_reconciled", "no_entry_fill_terminal"}
            for row in ledger.get("round_trips", [])
        ):
            raise AuditError("execution ledger contains an incomplete trade outcome")
        if intents.get("intents") and journal.get("run_status") != "RECONCILED":
            raise AuditError("broker journal is not reconciled")
        if not intents.get("intents") and journal.get("run_status") != "NO_TRADE":
            raise AuditError("empty intent bundle is not recorded as NO_TRADE")
        readiness = _read(root, "readiness_status.json")
        probability = readiness.get("probability", {})
        if (
            probability.get("probability_publication_allowed") is not False
            or probability.get("p_committee_hit") is not None
        ):
            raise AuditError("first-day readiness improperly enables probability publication")
        net_profit = readiness.get("net_profit", {})
        if (
            net_profit.get("net_profit_publication_allowed") is not False
            or net_profit.get("p_net_profit") is not None
        ):
            raise AuditError("first-day readiness improperly enables net-profit publication")
        if frozen.get("mode") == "canary":
            label_document = _read(root, "labels_provisional.json")
            if predictions or label_document.get("official_label_status") == "provisional":
                try:
                    labels = validate_label_document(
                        label_document,
                        frozen,
                        expected_phase="provisional",
                    )
                except LabelError as exc:
                    raise AuditError(f"invalid provisional labels: {exc}") from exc
                expected_label_status = "provisional"
            else:
                if (
                    set(label_document) != LABEL_DOCUMENT_KEYS
                    or label_document.get("schema_version") != 6
                    or label_document.get("run_id") != frozen.get("run_id")
                    or label_document.get("provider") != "not_applicable"
                    or label_document.get("adjustment") != "NONE"
                    or label_document.get("session_scope") != "US_regular_session"
                    or label_document.get("official_label_status")
                    != "not_applicable_no_forecasts"
                    or label_document.get("labels") != []
                ):
                    raise AuditError("zero-forecast label record differs from contract")
                labels = label_document
                expected_label_status = "not_applicable_no_forecasts"
            evaluations = _read(root, "evaluations_provisional.json")
            if (
                set(evaluations) != {
                    "schema_version", "run_id", "official_label_status", "evaluations"
                }
                or evaluations.get("schema_version") != 6
                or evaluations.get("run_id") != frozen.get("run_id")
                or evaluations.get("official_label_status") != expected_label_status
            ):
                raise AuditError("prospective evaluation metadata differs from contract")
            try:
                rebuilt_evaluations = (
                    build_prospective_evaluations(frozen, labels) if predictions else []
                )
            except ReadinessError as exc:
                raise AuditError(f"prospective evaluation cannot be rebuilt: {exc}") from exc
            if evaluations.get("evaluations") != rebuilt_evaluations:
                raise AuditError("prospective evaluations differ from deterministic recomputation")
            if any(row.get("p_committee_hit") is not None for row in evaluations.get("evaluations", [])):
                raise AuditError("unapproved probability entered the prospective ledger")
        checked.extend([
            "broker_reconciliation", "execution_ledger", "post_exit_portfolio",
            "provisional_labels", "prospective_probability_and_cost_ledger",
        ])
    result = {
        "schema_version": 6,
        "run_id": frozen["run_id"],
        "stage": stage,
        "status": "passed",
        "mode": frozen["mode"],
        "forecast_count": len(predictions),
        "intent_count": len(intents.get("intents", [])),
        "checks": checked,
        "run_sha256": declared_hash,
        "universe": universe,
        "candidate_intake": candidate_intake,
        "formal_ai_call_sha256": ai_call_hashes,
    }
    if stage == "complete":
        result["complete_artifact_sha256"] = {
            name: sha256_file(root / name)
            for name in (
                "broker_journal.json",
                "execution_ledger.json",
                "portfolio_post_exit.json",
                "labels_provisional.json",
                "evaluations_provisional.json",
                "readiness_status.json",
            )
        }
    return result
