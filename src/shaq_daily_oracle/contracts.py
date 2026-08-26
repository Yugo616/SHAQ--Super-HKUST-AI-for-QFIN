from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    """A public report violates the compact SHAQ interface."""


DOMAINS = {
    "market",
    "relationships",
    "event",
    "capital",
    "derivatives",
    "price_volume",
}
VERDICTS = {"bullish", "bearish", "neutral", "not_applicable", "unavailable"}
AVAILABILITIES = {"available", "no_data", "not_entitled", "provider_error"}
COMPONENT_TYPES = {
    "market_beta", "industry_spillover", "company_event", "capital_flow",
    "derivatives_distribution", "price_volume_state",
}
DEFAULT_COMPONENT = {
    "market": "market_beta", "relationships": "industry_spillover",
    "event": "company_event", "capital": "capital_flow",
    "derivatives": "derivatives_distribution", "price_volume": "price_volume_state",
}
REQUIRED = {
    "domain",
    "as_of_et",
    "horizon",
    "verdict",
    "thesis",
    "antithesis",
    "unknowns",
    "invalidation",
    "evidence_ids",
    "lineage_root_ids",
    "availability",
    "component_type",
}
FORBIDDEN = {
    "probability",
    "confidence",
    "strength",
    "score",
    "core_direction",
    "ranking",
    "label",
    "outcome",
    "next_return",
    "other_agent_reports",
}


def _walk_forbidden(value: Any, location: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN:
                raise ContractError(f"forbidden field {key!r} at {location}")
            _walk_forbidden(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{location}[{index}]")


def validate_domain_report(
    report: dict[str, Any], evidence_to_roots: dict[str, Any], evidence_domains: dict[str, Any]
) -> dict[str, Any]:
    report = dict(report)
    report.setdefault("availability", "available" if report.get("verdict") != "unavailable" else "no_data")
    report.setdefault("component_type", DEFAULT_COMPONENT.get(str(report.get("domain")), ""))
    _walk_forbidden(report)
    missing = REQUIRED.difference(report)
    extra = set(report).difference(REQUIRED)
    if missing or extra:
        raise ContractError(f"report keys differ: missing={sorted(missing)}, extra={sorted(extra)}")
    if report["domain"] not in DOMAINS or report["verdict"] not in VERDICTS:
        raise ContractError("invalid domain or verdict")
    if report["availability"] not in AVAILABILITIES or report["component_type"] not in COMPONENT_TYPES:
        raise ContractError("invalid availability or component_type")
    if (report["verdict"] == "unavailable") != (report["availability"] != "available"):
        raise ContractError("availability and verdict disagree")
    for key in ("as_of_et", "horizon", "thesis", "antithesis"):
        if not isinstance(report[key], str) or not report[key].strip():
            raise ContractError(f"{key} must be non-empty text")
    for key in ("unknowns", "invalidation", "evidence_ids", "lineage_root_ids"):
        if not isinstance(report[key], list) or len(report[key]) != len(set(report[key])):
            raise ContractError(f"{key} must be a unique list")
    roots: set[str] = set()
    for evidence_id in report["evidence_ids"]:
        if evidence_id not in evidence_to_roots:
            raise ContractError(f"unknown evidence_id: {evidence_id}")
        allowed = evidence_domains.get(evidence_id, set())
        allowed = {allowed} if isinstance(allowed, str) else set(allowed)
        if report["domain"] not in allowed:
            raise ContractError("domain report cites evidence outside its routed task")
        values = evidence_to_roots[evidence_id]
        roots.update([values] if isinstance(values, str) else values)
    if sorted(roots) != sorted(report["lineage_root_ids"]):
        raise ContractError("lineage_root_ids do not match verified evidence")
    if report["verdict"] in {"bullish", "bearish"} and not report["evidence_ids"]:
        raise ContractError("directional report requires verified evidence")
    return {key: report[key] for key in sorted(report)}


def validate_adversary_report(report: dict[str, Any]) -> dict[str, Any]:
    required = {
        "counts_as_vote",
        "new_evidence_allowed",
        "duplicate_lineage_roots",
        "unresolved_conflicts",
        "strongest_countercase",
        "veto",
        "veto_reason",
    }
    if set(report) != required:
        raise ContractError("adversary report keys differ from contract")
    if report["counts_as_vote"] is not False or report["new_evidence_allowed"] is not False:
        raise ContractError("adversary is non-voting and may not add evidence")
    if not isinstance(report["veto"], bool):
        raise ContractError("veto must be boolean")
    if not isinstance(report["strongest_countercase"], str) or not report["strongest_countercase"].strip():
        raise ContractError("strongest_countercase must be non-empty text")
    for key in ("duplicate_lineage_roots", "unresolved_conflicts"):
        if not isinstance(report[key], list) or len(report[key]) != len(set(report[key])):
            raise ContractError(f"{key} must be a unique list")
    if report["veto"] and (not isinstance(report["veto_reason"], str) or not report["veto_reason"].strip()):
        raise ContractError("a veto requires a reason")
    return {key: report[key] for key in sorted(report)}
