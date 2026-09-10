from __future__ import annotations

import hashlib
import json
from typing import Any

from .hashing import sha256_payload


class DecisionSandboxError(ValueError):
    """A Shadow decision rule exceeded its local research-only boundary."""


DECISION_SCRIPT_PATH = "decision/decision.js"
DECISION_CASES_PATH = "decision/cases.json"
MAX_SCRIPT_BYTES = 100_000
MAX_CASES_BYTES = 300_000
MAX_OUTPUT_BYTES = 256_000
MAX_MEMORY_BYTES = 16 * 1024 * 1024
MAX_STACK_BYTES = 512 * 1024
MAX_RUNTIME_SECONDS = 0.25
FORBIDDEN_INPUT_KEYS = {
    "actual_direction",
    "close_to_close_return",
    "correct",
    "evaluated",
    "final_ranking",
    "label",
    "labels",
    "next_return",
    "outcome",
    "pnl",
    "result_label",
    "win",
    "winner",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_text(value: str, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionSandboxError(f"{name} is empty")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum:
        raise DecisionSandboxError(f"{name} exceeds the size limit")
    if "\x00" in value:
        raise DecisionSandboxError(f"{name} contains a NUL byte")
    return value


def _reject_forbidden_keys(value: Any, *, trail: str = "input") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_INPUT_KEYS:
                raise DecisionSandboxError(
                    f"decision input contains forbidden result field: {trail}.{key}"
                )
            _reject_forbidden_keys(item, trail=f"{trail}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_keys(item, trail=f"{trail}[{index}]")


def build_decision_input(
    *,
    as_of_et: str,
    reports_by_symbol: dict[str, list[dict[str, Any]]],
    adversary_by_symbol: dict[str, dict[str, Any]],
    candidate_intake: dict[str, Any],
    root_component_types: dict[str, Any],
) -> dict[str, Any]:
    """Expose only frozen premarket conclusions needed by a decision rule."""

    candidates = []
    for row in candidate_intake.get("candidates", []):
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            raise DecisionSandboxError("candidate has no symbol")
        candidates.append({
            "symbol": symbol,
            "industry_group": str(row.get("gics_sector", "Unknown")) or "Unknown",
            "track": "event" if row.get("captured_primary_event") is True else "ordinary",
        })
    symbols = {row["symbol"] for row in candidates}
    if len(symbols) != len(candidates):
        raise DecisionSandboxError("decision candidates are empty or duplicated")
    if set(reports_by_symbol) != symbols or set(adversary_by_symbol) != symbols:
        raise DecisionSandboxError("decision reports differ from frozen candidates")
    reports: dict[str, list[dict[str, Any]]] = {}
    available_roots: set[str] = set()
    for symbol in sorted(symbols):
        reports[symbol] = []
        for report in reports_by_symbol[symbol]:
            roots = sorted({str(root) for root in report.get("lineage_root_ids", [])})
            available_roots.update(roots)
            reports[symbol].append({
                "domain": str(report.get("domain", "")),
                "availability": str(report.get("availability", "")),
                "verdict": str(report.get("verdict", "")),
                "lineage_root_ids": roots,
            })
        reports[symbol].sort(key=lambda item: item["domain"])
    root_types = {
        str(root): sorted({str(value) for value in values})
        for root, values in root_component_types.items()
        if str(root) in available_roots
    }
    decision_input = {
        "schema_version": 1,
        "as_of_et": str(as_of_et),
        "horizon": "official_US_regular_session_open_to_close",
        "candidates": sorted(candidates, key=lambda item: item["symbol"]),
        "reports_by_symbol": reports,
        "adversary_by_symbol": {
            symbol: {
                "veto": adversary_by_symbol[symbol].get("veto") is True,
                "strongest_countercase": str(
                    adversary_by_symbol[symbol].get("strongest_countercase", "")
                ),
                "veto_reason": str(adversary_by_symbol[symbol].get("veto_reason", "")),
            }
            for symbol in sorted(symbols)
        },
        "root_component_types": root_types,
    }
    _reject_forbidden_keys(decision_input)
    return decision_input


def _quickjs_context():
    try:
        import quickjs  # type: ignore
    except ImportError as exc:
        raise DecisionSandboxError("QuickJS decision sandbox is not installed") from exc
    context = quickjs.Context()
    context.set_memory_limit(MAX_MEMORY_BYTES)
    context.set_max_stack_size(MAX_STACK_BYTES)
    context.set_time_limit(MAX_RUNTIME_SECONDS)
    context.eval(
        """
        'use strict';
        globalThis.process = undefined;
        globalThis.require = undefined;
        globalThis.fetch = undefined;
        globalThis.XMLHttpRequest = undefined;
        globalThis.WebSocket = undefined;
        globalThis.Deno = undefined;
        globalThis.Bun = undefined;
        globalThis.Worker = undefined;
        globalThis.Date = undefined;
        Math.random = function () { throw new Error('randomness is disabled'); };
        Object.freeze(Math);
        """
    )
    return context


def decision_parameters(cases_text):
    value = json.loads(cases_text).get('parameters', {'maximum_predictions': 3})
    if not isinstance(value, dict) or set(value) != {'maximum_predictions'}:
        raise DecisionSandboxError('unsupported research decision parameters')
    maximum = value['maximum_predictions']
    if type(maximum) is not int or maximum < 0:
        raise DecisionSandboxError('maximum_predictions must be a nonnegative integer')
    return value


def decision_mode(cases_text: str) -> str:
    mode = json.loads(cases_text).get("mode", "rules")
    if mode not in {"rules", "synthesis"}:
        raise DecisionSandboxError("unsupported decision mode")
    return mode


def _validate_decision_output(
    value: Any, *, decision_input: dict[str, Any], citation_policy: str = "directional"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "decisions"}:
        raise DecisionSandboxError("decision output has unsupported fields")
    if value.get("schema_version") != 1 or not isinstance(value.get("decisions"), list):
        raise DecisionSandboxError("decision output schema is invalid")
    candidates = {row["symbol"]: row for row in decision_input["candidates"]}
    rows = value["decisions"]
    if len(rows) != len(candidates):
        raise DecisionSandboxError("decision output must include every candidate exactly once")
    allowed_fields = {"symbol", "action", "direction", "reason", "evidence_root_ids"}
    seen: set[str] = set()
    decisions = []
    predictions = []
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != allowed_fields:
            raise DecisionSandboxError("decision row fields are invalid")
        symbol = str(raw.get("symbol", "")).upper()
        if symbol not in candidates or symbol in seen:
            raise DecisionSandboxError("decision output contains an unknown or duplicate symbol")
        seen.add(symbol)
        action = str(raw.get("action", ""))
        direction = str(raw.get("direction", ""))
        reason = str(raw.get("reason", "")).strip()
        roots = raw.get("evidence_root_ids")
        if action not in {"publish", "reject"} or not reason or len(reason) > 500:
            raise DecisionSandboxError("decision action or reason is invalid")
        if not isinstance(roots, list) or len(roots) != len(set(map(str, roots))):
            raise DecisionSandboxError("decision evidence roots are invalid")
        roots = sorted(map(str, roots))
        reports = decision_input["reports_by_symbol"][symbol]
        allowed_directional_roots = {
            root
            for report in reports
            if report.get("availability") == "available"
            and (citation_policy == "available" or report.get("verdict") == direction)
            for root in report.get("lineage_root_ids", [])
        }
        if action == "publish":
            if direction not in {"bullish", "bearish"}:
                raise DecisionSandboxError("published decision must be bullish or bearish")
            if decision_input["adversary_by_symbol"][symbol]["veto"]:
                raise DecisionSandboxError("decision rule cannot bypass an adversary integrity veto")
            if not roots or not set(roots).issubset(allowed_directional_roots):
                raise DecisionSandboxError("published decision cites an unavailable or invented root")
            candidate = candidates[symbol]
            predictions.append({
                "symbol": symbol,
                "direction": direction,
                "track": candidate["track"],
                "score_eligible": True,
                "industry_group": candidate["industry_group"],
            })
        elif direction != "neutral" or roots:
            raise DecisionSandboxError("rejected decision must be neutral and cite no roots")
        decisions.append({
            "symbol": symbol,
            "action": action,
            "direction": direction,
            "reason": reason,
            "evidence_root_ids": roots,
        })
    limit = decision_input.get('research_parameters', {}).get('maximum_predictions', 3)
    if type(limit) is not int or limit < 0 or len(predictions) > limit:
        raise DecisionSandboxError("decision output exceeds the configured prediction cap")
    decisions.sort(key=lambda item: item["symbol"])
    predictions.sort(key=lambda item: item["symbol"])
    return decisions, predictions


def execute_decision_script(
    *, script: str, decision_input: dict[str, Any], citation_policy: str = "directional"
) -> dict[str, Any]:
    if citation_policy not in {"directional", "available"}:
        raise DecisionSandboxError("unsupported citation policy")
    script = _validate_text(script, name="decision script", maximum=MAX_SCRIPT_BYTES)
    _reject_forbidden_keys(decision_input)
    serialized_input = json.dumps(
        decision_input, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    context = _quickjs_context()
    context.set("__SHAQ_INPUT__", serialized_input)
    wrapped = (
        "'use strict';\n"
        + script
        + "\nif (typeof decide !== 'function') { throw new Error('decide function is required'); }\n"
        + "JSON.stringify(decide(JSON.parse(__SHAQ_INPUT__)));"
    )
    try:
        output_text = context.eval(wrapped)
    except Exception as exc:
        raise DecisionSandboxError(f"decision script interrupted or failed: {exc}") from exc
    if not isinstance(output_text, str):
        raise DecisionSandboxError("decision script returned no JSON result")
    if len(output_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise DecisionSandboxError("decision output exceeds the size limit")
    try:
        value = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise DecisionSandboxError("decision script returned invalid JSON") from exc
    decisions, predictions = _validate_decision_output(value, decision_input=decision_input, citation_policy=citation_policy)
    audit_unsigned = {
        "schema_version": 1,
        "engine": "quickjs-isolated",
        "script_sha256": _sha256_text(script),
        "input_sha256": sha256_payload(decision_input),
        "output_sha256": sha256_payload(value),
        "network_allowed": False,
        "filesystem_allowed": False,
        "system_commands_allowed": False,
        "broker_allowed": False,
        "result_fields_allowed": False,
        "maximum_runtime_seconds": MAX_RUNTIME_SECONDS,
        "maximum_memory_bytes": MAX_MEMORY_BYTES,
    }
    return {
        "schema_version": 1,
        "engine": "quickjs-isolated",
        "decisions": decisions,
        "predictions": predictions,
        "audit": {
            **audit_unsigned,
            "audit_sha256": sha256_payload(audit_unsigned),
        },
    }


def run_decision_cases(*, script: str, cases_text: str) -> dict[str, Any]:
    script = _validate_text(script, name="decision script", maximum=MAX_SCRIPT_BYTES)
    cases_text = _validate_text(cases_text, name="decision cases", maximum=MAX_CASES_BYTES)
    try:
        document = json.loads(cases_text)
    except json.JSONDecodeError as exc:
        raise DecisionSandboxError("decision cases are not valid JSON") from exc
    if not isinstance(document, dict) or set(document) - {"schema_version", "cases", "parameters", "mode"}:
        raise DecisionSandboxError("decision cases have unsupported fields")
    if document.get("schema_version") != 1 or not isinstance(document.get("cases"), list):
        raise DecisionSandboxError("decision cases schema is invalid")
    if not document["cases"]:
        raise DecisionSandboxError("at least one decision case is required")
    seen: set[str] = set()
    parameters = decision_parameters(cases_text)
    mode = decision_mode(cases_text)
    results = []
    for case in document["cases"]:
        if not isinstance(case, dict) or set(case) != {"name", "input", "expected_decisions"}:
            raise DecisionSandboxError("decision case fields are invalid")
        name = str(case.get("name", "")).strip()
        if not name or name in seen:
            raise DecisionSandboxError("decision case name is empty or duplicated")
        seen.add(name)
        result = execute_decision_script(script=script, decision_input={**case["input"], 'research_parameters': parameters},
            citation_policy="available" if mode == "synthesis" else "directional")
        if result["decisions"] != case["expected_decisions"]:
            raise DecisionSandboxError(f"decision case failed: {name}")
        results.append({"name": name, "status": "passed"})
    receipt_unsigned = {
        "schema_version": 1,
        "status": "passed",
        "case_count": len(results),
        "script_sha256": _sha256_text(script),
        "cases_sha256": _sha256_text(cases_text),
        "results": results,
    }
    return {
        **receipt_unsigned,
        "receipt_sha256": sha256_payload(receipt_unsigned),
    }
