from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from .hashing import sha256_payload


class PostmortemError(ValueError):
    """A retrospective record is incomplete, mutable, or leaks into prediction."""


DOMAIN_COMPONENT = {
    "market": "market_component_return",
    "relationships": "industry_component_return",
    "event": "stock_specific_component_return",
    "capital": "stock_specific_component_return",
    "derivatives": "stock_specific_component_return",
    "price_volume": "stock_specific_component_return",
}

AI_DIAGNOSTIC_CATEGORIES = {
    "evidence_missing",
    "information_already_absorbed",
    "horizon_mismatch",
    "gate_overcautious",
    "post_cutoff_shock",
    "evidence_interpretation",
    "unexplained",
}


def _parse_offset(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PostmortemError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PostmortemError(f"{field} requires an explicit offset")
    return parsed


def _direction(value: float) -> str:
    return "bullish" if value > 0 else ("bearish" if value < 0 else "neutral")


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PostmortemError(f"{field} is not numeric") from exc
    if not math.isfinite(number):
        raise PostmortemError(f"{field} is not finite")
    return number


def build_outcome_row(*, symbol: str, trade_date: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize one unadjusted RTH bar without assigning a forecast direction."""

    canonical = symbol.strip().upper()
    if not canonical:
        raise PostmortemError("outcome symbol is blank")
    matching = [row for row in rows if str(row.get("time_key", ""))[:10] == trade_date]
    if len(matching) != 1:
        raise PostmortemError("exactly one matching regular-session bar is required")
    row = matching[0]
    values = {
        field: _finite(row.get(field), field=f"{canonical}.{field}")
        for field in ("open", "high", "low", "close", "volume")
    }
    if (
        min(values["open"], values["high"], values["low"], values["close"]) <= 0
        or values["volume"] < 0
        or values["high"] < max(values["open"], values["close"], values["low"])
        or values["low"] > min(values["open"], values["close"], values["high"])
    ):
        raise PostmortemError("outcome OHLCV relationships are invalid")
    realized = values["close"] / values["open"] - 1.0
    return {
        "symbol": canonical,
        "trade_date": trade_date,
        "session_scope": "US_regular_session",
        "price_basis": "unadjusted_OHLC",
        "official_open": values["open"],
        "official_high": values["high"],
        "official_low": values["low"],
        "official_close": values["close"],
        "official_volume": values["volume"],
        "official_open_to_close_return": realized,
        "actual_direction": _direction(realized),
    }


def build_outcome_document(
    *, run_id: str, frozen_run_sha256: str, trade_date: str, phase: str,
    captured_at_et: str, session_close_et: str, rows_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if phase not in {"provisional", "final"}:
        raise PostmortemError("outcome phase must be provisional or final")
    target = date.fromisoformat(trade_date)
    captured = _parse_offset(captured_at_et, field="captured_at_et")
    session_close = _parse_offset(session_close_et, field="session_close_et")
    if session_close.date() != target or captured < session_close:
        raise PostmortemError("outcomes cannot be observed before the target session close")
    if phase == "provisional" and captured.date() != target:
        raise PostmortemError("provisional outcomes cannot be backfilled on a later date")
    if phase == "final" and captured.date() <= target:
        raise PostmortemError("final outcomes require a later-date independent reobservation")
    if not rows_by_symbol:
        raise PostmortemError("outcome universe is empty")
    outcomes = {
        symbol.strip().upper(): build_outcome_row(
            symbol=symbol, trade_date=trade_date, rows=rows
        )
        for symbol, rows in sorted(rows_by_symbol.items())
    }
    unsigned = {
        "schema_version": 1,
        "run_id": run_id,
        "frozen_run_sha256": frozen_run_sha256,
        "phase": phase,
        "provider": "Futu OpenD",
        "captured_at_et": captured.isoformat(),
        "trade_date": trade_date,
        "session_close_et": session_close.isoformat(),
        "adjustment": "NONE",
        "session_scope": "US_regular_session",
        "candidate_outcomes_are_not_forecast_labels": True,
        "rows_by_symbol": outcomes,
    }
    return {**unsigned, "outcome_document_sha256": sha256_payload(unsigned)}


def validate_outcome_document(document: dict[str, Any], frozen: dict[str, Any]) -> dict[str, Any]:
    declared = document.get("outcome_document_sha256")
    unsigned = {key: value for key, value in document.items() if key != "outcome_document_sha256"}
    if declared != sha256_payload(unsigned):
        raise PostmortemError("outcome document hash mismatch")
    if (
        document.get("run_id") != frozen.get("run_id")
        or document.get("frozen_run_sha256") != frozen.get("run_sha256")
        or document.get("adjustment") != "NONE"
        or document.get("session_scope") != "US_regular_session"
        or document.get("candidate_outcomes_are_not_forecast_labels") is not True
    ):
        raise PostmortemError("outcome document is not bound to the frozen run")
    return document


def _validate_frozen_hash(frozen: dict[str, Any]) -> None:
    declared = frozen.get("run_sha256")
    unsigned = {key: value for key, value in frozen.items() if key != "run_sha256"}
    if declared != sha256_payload(unsigned):
        raise PostmortemError("frozen run hash mismatch")


def _domain_diagnostic(report: dict[str, Any], attribution: dict[str, Any]) -> dict[str, Any]:
    domain = str(report.get("domain"))
    component = DOMAIN_COMPONENT.get(domain)
    if component is None:
        raise PostmortemError("postmortem received an unknown domain")
    realized = float(attribution[component])
    realized_direction = _direction(realized)
    availability = str(report.get("availability"))
    verdict = str(report.get("verdict"))
    if availability != "available" or verdict == "unavailable":
        diagnostic = "evidence_missing"
    elif verdict == "not_applicable":
        diagnostic = "not_applicable"
    elif verdict == "neutral":
        diagnostic = (
            "neutral_on_flat_component"
            if realized_direction == "neutral"
            else "neutral_with_realized_component"
        )
    elif realized_direction == "neutral":
        diagnostic = "directional_on_flat_component"
    elif verdict == realized_direction:
        diagnostic = "direction_aligned"
    else:
        diagnostic = "direction_error"
    return {
        "domain": domain,
        "component": component,
        "premarket_verdict": verdict,
        "availability": availability,
        "realized_component_return": realized,
        "realized_component_direction": realized_direction,
        "diagnostic": diagnostic,
    }


def _validate_ai_hypotheses(
    hypotheses: list[dict[str, Any]], *, symbols: set[str], approved_reference_ids: set[str],
    post_cutoff_source_ids: set[str],
) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for row in hypotheses:
        required = {
            "hypothesis_id", "symbol", "diagnostic_category", "economic_mechanism",
            "affected_domains", "expected_improvement", "invalidation_conditions",
            "reference_ids", "source_ids", "alternative_explanations",
            "strongest_countercase",
        }
        if set(row) != required:
            raise PostmortemError("AI hypothesis keys differ from the retrospective contract")
        hypothesis_id = str(row["hypothesis_id"]).strip()
        symbol = str(row["symbol"]).upper()
        category = str(row["diagnostic_category"])
        references = {str(value) for value in row["reference_ids"]}
        sources = {str(value) for value in row["source_ids"]}
        domains = {str(value) for value in row["affected_domains"]}
        for field in (
            "affected_domains", "reference_ids", "source_ids",
            "invalidation_conditions", "alternative_explanations",
        ):
            values = row[field]
            if not isinstance(values, list) or len(values) != len({str(value) for value in values}):
                raise PostmortemError(f"AI hypothesis {field} contains duplicates or is invalid")
        if not hypothesis_id or hypothesis_id in seen:
            raise PostmortemError("AI hypothesis IDs are blank or duplicated")
        if symbol not in symbols or category not in AI_DIAGNOSTIC_CATEGORIES:
            raise PostmortemError("AI hypothesis identity is invalid")
        if not references.issubset(approved_reference_ids):
            raise PostmortemError("AI hypothesis cited an unapproved research reference")
        if not sources.issubset(post_cutoff_source_ids):
            raise PostmortemError("AI hypothesis cited an unknown post-cutoff source")
        if not domains.issubset(DOMAIN_COMPONENT):
            raise PostmortemError("AI hypothesis cited an unknown domain")
        if category == "post_cutoff_shock" and not sources:
            raise PostmortemError("post-cutoff-shock hypothesis requires a captured source")
        for field in (
            "economic_mechanism", "expected_improvement", "strongest_countercase"
        ):
            if not str(row[field]).strip():
                raise PostmortemError(f"AI hypothesis {field} is blank")
        for field in ("invalidation_conditions", "alternative_explanations"):
            if not isinstance(row[field], list) or not all(str(value).strip() for value in row[field]):
                raise PostmortemError(f"AI hypothesis {field} is invalid")
        seen.add(hypothesis_id)
        output.append({
            **row,
            "symbol": symbol,
            "affected_domains": sorted(domains),
            "reference_ids": sorted(references),
            "source_ids": sorted(sources),
            "status": "internal_shadow_research_only",
            "causal_claim_policy": "hypothesis_not_fact",
            "automatic_mutation_allowed": False,
        })
    return sorted(output, key=lambda row: (row["symbol"], row["hypothesis_id"]))


def build_postmortem(
    *, frozen: dict[str, Any], candidate_intake: dict[str, Any],
    relationships_by_symbol: dict[str, dict[str, Any]], outcomes: dict[str, Any],
    generated_at_et: str, approved_reference_ids: set[str],
    post_cutoff_sources: list[dict[str, Any]] | None = None,
    ai_hypotheses: list[dict[str, Any]] | None = None,
    postmortem_pipeline_identity: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic retrospective diagnostic that cannot update production."""

    _validate_frozen_hash(frozen)
    validate_outcome_document(outcomes, frozen)
    generated = _parse_offset(generated_at_et, field="generated_at_et")
    captured = _parse_offset(outcomes["captured_at_et"], field="outcomes.captured_at_et")
    if generated < captured:
        raise PostmortemError("postmortem predates its outcome observation")
    candidates = {
        str(row["symbol"]).upper(): row for row in candidate_intake.get("candidates", [])
    }
    if set(candidates) != set(frozen.get("reports_by_symbol", {})):
        raise PostmortemError("postmortem candidates differ from the frozen reviewed set")
    observed = outcomes.get("rows_by_symbol", {})
    required_symbols = {"SPY", *candidates}
    required_symbols.update(str(row.get("sector_benchmark", "")).upper() for row in candidates.values())
    required_symbols.discard("")
    if not required_symbols.issubset(observed):
        raise PostmortemError("postmortem outcomes lack a candidate or benchmark")

    predictions = {row["symbol"]: row for row in frozen.get("predictions", [])}
    rows = []
    for symbol, candidate in sorted(candidates.items()):
        relationship = relationships_by_symbol.get(symbol)
        if not relationship:
            raise PostmortemError(f"{symbol} lacks frozen relationship exposure")
        sector = str(candidate.get("sector_benchmark", "")).upper()
        if relationship.get("sector_benchmark") != sector:
            raise PostmortemError(f"{symbol} sector exposure differs from frozen intake")
        exposures = relationship.get("multi_etf_beta_126", {})
        market_beta = _finite(exposures.get("SPY"), field=f"{symbol}.market_beta_126")
        sector_beta = _finite(relationship.get("sector_beta"), field=f"{symbol}.sector_beta_126")
        stock_return = _finite(
            observed[symbol].get("official_open_to_close_return"), field=f"{symbol}.return"
        )
        market_return = _finite(
            observed["SPY"].get("official_open_to_close_return"), field="SPY.return"
        )
        sector_return = _finite(
            observed[sector].get("official_open_to_close_return"), field=f"{sector}.return"
        )
        market_component = market_beta * market_return
        sector_residual = sector_return - market_return
        industry_component = sector_beta * sector_residual
        stock_specific = stock_return - market_component - industry_component
        attribution = {
            "stock_open_to_close_return": stock_return,
            "market_symbol": "SPY",
            "market_open_to_close_return": market_return,
            "market_beta_126_frozen": market_beta,
            "market_component_return": market_component,
            "sector_symbol": sector,
            "sector_open_to_close_return": sector_return,
            "sector_minus_market_return": sector_residual,
            "sector_beta_126_frozen": sector_beta,
            "industry_component_return": industry_component,
            "stock_specific_component_return": stock_specific,
            "decomposition_identity_error": stock_return - market_component - industry_component - stock_specific,
            "estimation_policy": "T_minus_1_frozen_exposures_no_same_day_refit",
            "causal_interpretation": "diagnostic_attribution_not_unique_cause",
        }
        if abs(attribution["decomposition_identity_error"]) > 1e-12:
            raise PostmortemError("return decomposition does not close")
        diagnostics = [
            _domain_diagnostic(report, attribution)
            for report in sorted(frozen["reports_by_symbol"][symbol], key=lambda row: row["domain"])
        ]
        prediction = predictions.get(symbol)
        actual_direction = observed[symbol]["actual_direction"]
        rows.append({
            "symbol": symbol,
            "published": prediction is not None,
            "published_direction": prediction.get("direction") if prediction else None,
            "published_correct": (
                prediction.get("direction") == actual_direction if prediction else None
            ),
            "actual_direction": actual_direction,
            "uncovered_realized_move": prediction is None and actual_direction != "neutral",
            "integration_rejection_reasons": frozen.get("integration_audit_by_symbol", {}).get(
                symbol, {}
            ).get("rejection_reasons", []),
            "attribution": attribution,
            "domain_diagnostics": diagnostics,
        })

    sources = post_cutoff_sources or []
    source_ids = [str(row.get("source_id", "")) for row in sources]
    if "" in source_ids or len(source_ids) != len(set(source_ids)):
        raise PostmortemError("post-cutoff source IDs are blank or duplicated")
    cutoff = _parse_offset(frozen["cutoff_et"], field="frozen.cutoff_et")
    session_close = _parse_offset(outcomes["session_close_et"], field="outcomes.session_close_et")
    for source in sources:
        published = _parse_offset(source.get("published_at_et"), field="source.published_at_et")
        if not cutoff < published <= session_close:
            raise PostmortemError("post-cutoff source is outside the retrospective event window")
    hypotheses = _validate_ai_hypotheses(
        ai_hypotheses or [], symbols=set(candidates),
        approved_reference_ids=approved_reference_ids,
        post_cutoff_source_ids=set(source_ids),
    )
    unsigned = {
        "schema_version": 1,
        "run_id": frozen["run_id"],
        "frozen_run_sha256": frozen["run_sha256"],
        "system_identity": frozen["system_identity"],
        "system_config_sha256": frozen["system_config_sha256"],
        **({"postmortem_pipeline_identity": postmortem_pipeline_identity}
           if postmortem_pipeline_identity else {}),
        "phase": outcomes["phase"],
        "trade_date": outcomes["trade_date"],
        "generated_at_et": generated.isoformat(),
        "outcome_document_sha256": outcomes["outcome_document_sha256"],
        "candidate_count": len(rows),
        "candidate_diagnostics": rows,
        "post_cutoff_sources": sorted(sources, key=lambda row: row["source_id"]),
        "learning_hypotheses": hypotheses,
        "automatic_production_mutation_allowed": False,
        "prediction_runtime_read_allowed": False,
        "daily_result_can_change_skill_or_gate": False,
        "research_review_policy": "batch_review_only",
        "causal_claim_policy": "attribution_hypotheses_are_not_ground_truth",
    }
    return {**unsigned, "postmortem_sha256": sha256_payload(unsigned)}


def validate_postmortem(document: dict[str, Any], frozen: dict[str, Any]) -> dict[str, Any]:
    declared = document.get("postmortem_sha256")
    unsigned = {key: value for key, value in document.items() if key != "postmortem_sha256"}
    if declared != sha256_payload(unsigned):
        raise PostmortemError("postmortem hash mismatch")
    if (
        document.get("run_id") != frozen.get("run_id")
        or document.get("frozen_run_sha256") != frozen.get("run_sha256")
        or document.get("automatic_production_mutation_allowed") is not False
        or document.get("prediction_runtime_read_allowed") is not False
        or document.get("daily_result_can_change_skill_or_gate") is not False
    ):
        raise PostmortemError("postmortem violates the learning-isolation contract")
    return document
