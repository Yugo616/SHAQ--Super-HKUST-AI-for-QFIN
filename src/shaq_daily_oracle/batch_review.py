from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from .hashing import sha256_payload
from .postmortem import PostmortemError


def _validate_document(document: dict[str, Any]) -> None:
    declared = document.get("postmortem_sha256")
    unsigned = {key: value for key, value in document.items() if key != "postmortem_sha256"}
    if declared != sha256_payload(unsigned):
        raise PostmortemError("batch review received a modified postmortem")
    if (
        document.get("phase") != "final"
        or document.get("automatic_production_mutation_allowed") is not False
        or document.get("prediction_runtime_read_allowed") is not False
    ):
        raise PostmortemError("batch review accepts only isolated final postmortems")


def adwin_mean_shift(values: list[float], *, delta: float) -> dict[str, Any]:
    """One-pass ADWIN-style cut test; it emits an alert and never retrains a model."""

    if not 0 < delta < 1:
        raise PostmortemError("drift delta must be between zero and one")
    if any(not math.isfinite(value) for value in values):
        raise PostmortemError("drift series contains a non-finite value")
    if len(values) < 2:
        return {"alert": False, "cut_index": None, "mean_before": None, "mean_after": None}
    overall_mean = sum(values) / len(values)
    variance = sum((value - overall_mean) ** 2 for value in values) / len(values)
    log_term = math.log(2.0 / delta)
    best = None
    for cut in range(1, len(values)):
        left, right = values[:cut], values[cut:]
        left_mean = sum(left) / len(left)
        right_mean = sum(right) / len(right)
        reciprocal = 1.0 / len(left) + 1.0 / len(right)
        boundary = math.sqrt(2.0 * variance * reciprocal * log_term)
        boundary += (2.0 / 3.0) * reciprocal * log_term
        difference = abs(left_mean - right_mean)
        if difference > boundary and (best is None or difference - boundary > best[0]):
            best = (difference - boundary, cut, left_mean, right_mean)
    return {
        "alert": best is not None,
        "cut_index": best[1] if best else None,
        "mean_before": best[2] if best else None,
        "mean_after": best[3] if best else None,
    }


def build_batch_review(
    *, postmortems: list[dict[str, Any]], generated_at_et: str,
    review_interval_sessions: int, drift_delta: float,
    minimum_promotion_days: int, minimum_promotion_forecasts: int,
) -> dict[str, Any]:
    if review_interval_sessions <= 0:
        raise PostmortemError("review interval must be positive")
    generated = datetime.fromisoformat(generated_at_et.replace("Z", "+00:00"))
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise PostmortemError("batch review time requires an explicit offset")
    ordered = sorted(postmortems, key=lambda row: row.get("trade_date", ""))
    dates = [date.fromisoformat(str(row.get("trade_date"))) for row in ordered]
    if len(dates) != len(set(dates)):
        raise PostmortemError("batch review contains duplicate sessions")
    for document in ordered:
        _validate_document(document)
    diagnostics = Counter()
    hypotheses: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    published_count = 0
    correct_count = 0
    daily_uncovered_rates = []
    for document in ordered:
        candidates = document.get("candidate_diagnostics", [])
        uncovered = 0
        for candidate in candidates:
            if candidate.get("published"):
                published_count += 1
                correct_count += int(candidate.get("published_correct") is True)
            if candidate.get("uncovered_realized_move"):
                uncovered += 1
            for diagnostic in candidate.get("domain_diagnostics", []):
                diagnostics[(diagnostic.get("domain"), diagnostic.get("diagnostic"))] += 1
        daily_uncovered_rates.append(uncovered / len(candidates) if candidates else 0.0)
        for hypothesis in document.get("learning_hypotheses", []):
            key = (
                hypothesis.get("diagnostic_category"),
                tuple(hypothesis.get("affected_domains", [])),
                tuple(hypothesis.get("reference_ids", [])),
            )
            hypotheses[key].append(hypothesis)
    repeated = []
    for key, rows in hypotheses.items():
        if len(rows) < 2:
            continue
        repeated.append({
            "diagnostic_category": key[0],
            "affected_domains": list(key[1]),
            "reference_ids": list(key[2]),
            "occurrences": len(rows),
            "symbols": sorted({str(row.get("symbol")) for row in rows}),
            "status": "eligible_for_human_preregistered_challenger_design",
            "automatic_core_change_allowed": False,
        })
    sample_gates = {
        "minimum_trading_days": minimum_promotion_days,
        "observed_trading_days": len(dates),
        "minimum_evaluated_forecasts": minimum_promotion_forecasts,
        "observed_evaluated_forecasts": published_count,
        "proper_score_pairing_passed": False,
        "block_bootstrap_passed": False,
        "coverage_not_reduced_to_manufacture_improvement": False,
        "concentration_checks_passed": False,
    }
    unsigned = {
        "schema_version": 1,
        "generated_at_et": generated.isoformat(),
        "review_interval_sessions": review_interval_sessions,
        "reviewed_session_dates": [value.isoformat() for value in dates],
        "final_postmortem_sha256s": [row["postmortem_sha256"] for row in ordered],
        "candidate_diagnostic_clusters": [
            {"domain": key[0], "diagnostic": key[1], "occurrences": count}
            for key, count in sorted(diagnostics.items())
        ],
        "repeated_research_hypotheses": sorted(
            repeated,
            key=lambda row: (
                -row["occurrences"], row["diagnostic_category"], row["affected_domains"]
            ),
        ),
        "published_forecasts": published_count,
        "published_correct": correct_count,
        "observed_accuracy": correct_count / published_count if published_count else None,
        "risk_coverage_diagnostic": {
            "daily_uncovered_move_rates": daily_uncovered_rates,
            "mean_uncovered_move_rate": (
                sum(daily_uncovered_rates) / len(daily_uncovered_rates)
                if daily_uncovered_rates else None
            ),
        },
        "drift_alert": {
            **adwin_mean_shift(daily_uncovered_rates, delta=drift_delta),
            "delta": drift_delta,
            "effect": "inspection_alert_only_no_automatic_training",
        },
        "promotion_gate": {
            **sample_gates,
            "promotion_allowed": all(sample_gates.values()),
        },
        "formal_core_mutation_performed": False,
        "challenger_execution_policy": "shadow_only_after_human_preregistration",
    }
    return {**unsigned, "batch_review_sha256": sha256_payload(unsigned)}
