from __future__ import annotations

from typing import Any

from .contracts import DOMAINS
from .isolation import IsolationError, validate_isolation_status


def build_no_ai_run_input(
    *,
    run_id: str,
    created_at: str,
    cutoff_et: str,
    candidate_intake: dict[str, Any],
    evidence_manifest: dict[str, Any] | list[dict[str, Any]],
    isolation_status: dict[str, Any],
) -> dict[str, Any]:
    isolation = validate_isolation_status(isolation_status)
    if isolation["formal_ai_enabled"] is not False:
        raise IsolationError("the no-AI fallback is only valid when formal AI is disabled")
    candidates = candidate_intake.get("candidates", [])
    symbols = [str(row.get("symbol", "")).strip().upper() for row in candidates]
    if "" in symbols or len(symbols) != len(set(symbols)):
        raise ValueError("candidate intake has blank or duplicate symbols")
    evidence = (
        evidence_manifest.get("evidence", [])
        if isinstance(evidence_manifest, dict)
        else evidence_manifest
    )
    reports_by_symbol: dict[str, list[dict[str, Any]]] = {}
    adversary_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted(symbols):
        reports_by_symbol[symbol] = [
            {
                "domain": domain,
                "as_of_et": created_at,
                "horizon": "official_US_regular_session_open_to_close",
                "verdict": "unavailable",
                "thesis": "Formal AI is disabled; no domain judgment was generated.",
                "antithesis": "No cutoff-safe inference result exists for this domain.",
                "unknowns": ["domain judgment unavailable under the isolation policy"],
                "invalidation": [],
                "evidence_ids": [],
                "lineage_root_ids": [],
            }
            for domain in sorted(DOMAINS)
        ]
        adversary_by_symbol[symbol] = {
            "counts_as_vote": False,
            "new_evidence_allowed": False,
            "duplicate_lineage_roots": [],
            "unresolved_conflicts": ["formal AI isolation unavailable"],
            "strongest_countercase": "No isolated six-domain judgment exists.",
            "veto": True,
            "veto_reason": "formal AI disabled by the evidence-isolation gate",
        }
    return {
        "run_id": run_id,
        "created_at": created_at,
        "cutoff_et": cutoff_et,
        "evidence": evidence,
        "reports_by_symbol": reports_by_symbol,
        "adversary_by_symbol": adversary_by_symbol,
        "predictions": [],
    }

