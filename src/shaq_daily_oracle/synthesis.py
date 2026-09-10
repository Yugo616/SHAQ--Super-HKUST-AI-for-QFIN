"""Opt-in research synthesis. Financial judgement is distinct from evidence validity."""
from __future__ import annotations

import json
from typing import Any

from jsonschema import validate

from .decision_sandbox import _reject_forbidden_keys


def synthesis_schema(symbols: list[str]) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1, "maxLength": 500}
    strings = {"type": "array", "items": {"type": "string"}}
    properties = {
        "symbol": {"type": "string", "enum": sorted(symbols)},
        "action": {"type": "string", "enum": ["publish", "reject"]},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        **{key: text for key in ("thesis", "antithesis", "resolution", "comparison")},
        **{key: strings for key in ("unknowns", "invalidation", "evidence_ids")},
    }
    return {"type": "object", "additionalProperties": False, "required": ["decisions"],
        "properties": {"decisions": {"type": "array", "items": {"type": "object",
            "additionalProperties": False, "required": list(properties), "properties": properties}}}}


def synthesis_prompt(*, reports, adversary, documents, as_of_et, maximum_predictions):
    packet = {"as_of_et": as_of_et, "reports_by_symbol": reports, "adversary_by_symbol": adversary}
    _reject_forbidden_keys(packet)
    return (
        "You are SHAQ's final research synthesizer, not another evidence vote. Use only the supplied "
        "reports; no browsing, tools, outside knowledge or invented facts. The target is the official "
        "US regular session open-to-close return, not the already realized premarket move. "
        "Assess company-specific and common components jointly. Neutral market context and ordinary "
        "disagreement are not automatic rejection. Explain why the main mechanism outweighs the "
        "strongest countercase, or abstain if it does not. Do not count agents or evidence roots as "
        "strength. Missing data remains missing. Integrity vetoes cannot be bypassed. "
        f"Select at most {maximum_predictions} candidates, possibly none, and compare the selections "
        "with alternatives. Return every candidate exactly once. Cite only that candidate's evidence_ids "
        "from available reports. For rejects use neutral direction. No probabilities or confidence scores. "
        "Write thesis, antithesis, resolution, comparison, unknowns and invalidation in concise Chinese.\n\n"
        f"METHOD:\n{documents['skills/daily-oracle/SKILL.md']}\n\n"
        f"REFERENCES:\n{documents.get('skills/daily-oracle/references/foundations.md', '')}\n\n"
        f"FROZEN SYNTHESIS INPUT:\n{json.dumps(packet, sort_keys=True, ensure_ascii=False)}"
    )


def validate_synthesis(value, reports, evidence_to_roots, *, maximum_predictions):
    try:
        validate(value, synthesis_schema(list(reports)))
    except Exception as exc:
        raise ValueError(f"invalid synthesis structure: {exc}") from exc
    rows = value["decisions"]
    if len(rows) != len(reports) or {r["symbol"] for r in rows} != set(reports):
        raise ValueError("synthesis must cover each frozen candidate exactly once")
    if sum(r["action"] == "publish" for r in rows) > maximum_predictions:
        raise ValueError("synthesis exceeds prediction cap")
    decisions = []
    for row in rows:
        if any(not row[key].strip() for key in ("thesis", "antithesis", "resolution", "comparison")):
            raise ValueError("synthesis needs substantive reasons")
        allowed = {eid for report in reports[row["symbol"]] if report.get("availability") == "available"
            for eid in report.get("evidence_ids", [])}
        ids = row["evidence_ids"]
        if len(ids) != len(set(ids)) or not set(ids).issubset(allowed):
            raise ValueError("synthesis cites unavailable or invented evidence")
        roots = sorted({root for eid in ids for root in evidence_to_roots.get(eid, [])})
        if row["action"] == "publish":
            if row["direction"] == "neutral" or not roots or not row["invalidation"]:
                raise ValueError("published synthesis needs direction, evidence and invalidation")
        elif row["direction"] != "neutral":
            raise ValueError("rejected synthesis must be neutral")
        decisions.append({"symbol": row["symbol"], "action": row["action"], "direction": row["direction"],
            "reason": row["resolution"], "evidence_root_ids": roots if row["action"] == "publish" else []})
    return {"schema_version": 1, "decisions": decisions}
