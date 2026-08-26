from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import DOMAINS, validate_adversary_report, validate_domain_report
from .hashing import sha256_payload
from .isolation import validate_isolation_status
from .lineage import build_lineage_graph


class RunError(ValueError):
    """A SHAQ run cannot be frozen."""


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunError("run timestamps require an offset")
    return parsed


def freeze_run(
    *,
    run_input: dict[str, Any],
    evidence_root: str | Path,
    integration_policy: dict[str, Any],
    candidate_intake_sha256: str,
    isolation_status: dict[str, Any],
) -> dict[str, Any]:
    created_at = str(run_input["created_at"])
    cutoff_et = str(run_input["cutoff_et"])
    publication_deadline_et = str(run_input.get("publication_deadline_et", cutoff_et))
    formal_eligibility = run_input.get("formal_eligibility", True) is True
    if formal_eligibility and _time(publication_deadline_et) < _time(cutoff_et):
        raise RunError("publication deadline cannot precede the evidence cutoff")
    mode = (
        "canary"
        if formal_eligibility and _time(created_at) <= _time(publication_deadline_et)
        else "shadow"
    )
    if len(candidate_intake_sha256) != 64 or candidate_intake_sha256.lower() != candidate_intake_sha256:
        raise RunError("candidate intake SHA-256 is invalid")
    isolation = validate_isolation_status(isolation_status)
    graph = build_lineage_graph(run_input["evidence"], evidence_root, cutoff_et)
    bindings = integration_policy.get("parameter_bindings", {})
    required_bindings = [
        "minimum_aligned_independent_roots",
        "maximum_opposed_independent_roots",
        "maximum_predictions",
    ]
    modern_policy = "minimum_aligned_applicable_domains" in integration_policy
    if modern_policy:
        required_bindings += [
            "minimum_aligned_applicable_domains", "market_or_industry_root_component_types",
            "stock_specific_root_component_types",
        ]
    else:
        required_bindings.append("required_aligned_domain_groups")
    for name in required_bindings:
        if len(bindings.get(name, [])) != 3:
            raise RunError(f"integration policy lacks bindings for {name}")
    minimum_aligned = int(integration_policy["minimum_aligned_independent_roots"])
    minimum_domains = int(integration_policy.get("minimum_aligned_applicable_domains", 2))
    maximum_opposed = int(integration_policy["maximum_opposed_independent_roots"])
    maximum_predictions = int(integration_policy["maximum_predictions"])
    context_types = set(integration_policy.get("market_or_industry_root_component_types", ["market_context", "industry_context"]))
    stock_types = set(integration_policy.get("stock_specific_root_component_types", ["stock_event", "stock_capital", "stock_derivatives", "stock_price_volume"]))
    if minimum_aligned < 1 or minimum_domains < 2 or maximum_opposed < 0 or maximum_predictions < 0:
        raise RunError("integration root gates are invalid")
    if not context_types or not stock_types or context_types & stock_types:
        raise RunError("integration root component roles are invalid")
    captured_times = [_time(record["captured_at"]) for record in graph["records"]]
    if captured_times and _time(created_at) < max(captured_times):
        raise RunError("run creation time cannot precede frozen evidence capture")
    reports_by_symbol = run_input["reports_by_symbol"]
    adversary_by_symbol = run_input["adversary_by_symbol"]
    normalized_reports = {}
    normalized_adversary = {}
    from .tasks import DOMAIN_EVIDENCE_ROUTES
    evidence_domains = {
        record["evidence_id"]: set(record.get("consumer_domains", [record["domain"]]))
        for record in graph["records"]
    }
    for symbol in sorted(reports_by_symbol):
        reports = reports_by_symbol[symbol]
        domains = [report.get("domain") for report in reports]
        if len(reports) != 6 or len(set(domains)) != 6:
            raise RunError(f"{symbol} requires exactly six distinct domain reports")
        normalized_reports[symbol] = sorted(
            (
                validate_domain_report(report, graph["evidence_to_roots"], evidence_domains)
                for report in reports
            ),
            key=lambda report: report["domain"],
        )
        if any(_time(report["as_of_et"]) > _time(cutoff_et) for report in reports):
            raise RunError(f"{symbol} contains a post-cutoff domain report")
        if symbol not in adversary_by_symbol:
            raise RunError(f"{symbol} lacks adversary review")
        normalized_adversary[symbol] = validate_adversary_report(adversary_by_symbol[symbol])

    predictions = sorted(run_input["predictions"], key=lambda row: row["symbol"])
    if predictions and isolation["formal_ai_enabled"] is not True:
        raise RunError("formal AI is disabled; an AI-derived prediction cannot be frozen")
    if len(predictions) > maximum_predictions or len({row["symbol"] for row in predictions}) != len(predictions):
        raise RunError("predictions exceed the governed cap or contain duplicate symbols")
    normalized_predictions = []
    allowed_prediction_keys = {
        "symbol", "direction", "track", "score_eligible", "industry_group"
    }
    for prediction in predictions:
        if set(prediction) != allowed_prediction_keys:
            raise RunError("prediction keys differ from the frozen public contract")
        symbol = prediction["symbol"]
        if symbol not in normalized_reports or prediction.get("direction") not in {"bullish", "bearish"}:
            raise RunError("prediction lacks a six-domain packet or valid direction")
        if prediction.get("track") not in {"ordinary", "event"} or prediction.get("score_eligible") is not True:
            raise RunError("prediction requires an eligible ordinary or event track")
        if not isinstance(prediction.get("industry_group"), str) or not prediction["industry_group"].strip():
            raise RunError("prediction requires a point-in-time industry group")
        if normalized_adversary[symbol]["veto"]:
            raise RunError("vetoed symbol cannot be published")
        relative_states = {}
        aligned_roots: set[str] = set()
        opposed_roots: set[str] = set()
        aligned_roots_by_domain: dict[str, set[str]] = {}
        opposite = "bearish" if prediction["direction"] == "bullish" else "bullish"
        for report in normalized_reports[symbol]:
            verdict = report["verdict"]
            state = (
                "aligned" if verdict == prediction["direction"]
                else ("opposed" if verdict == opposite else verdict)
            )
            relative_states[report["domain"]] = state
            if state == "aligned":
                report_roots = set(report["lineage_root_ids"])
                aligned_roots.update(report_roots)
                aligned_roots_by_domain[report["domain"]] = report_roots
            elif state == "opposed":
                opposed_roots.update(report["lineage_root_ids"])
        conflicted = aligned_roots.intersection(opposed_roots)
        clean_aligned = aligned_roots - conflicted
        clean_opposed = opposed_roots - conflicted
        aligned_domains = {
            domain
            for domain, roots in aligned_roots_by_domain.items()
            if roots.intersection(clean_aligned)
        }
        root_components = {key: set(value) for key, value in graph["root_component_types"].items()}
        context_roots = sorted(root for root in clean_aligned if root_components.get(root, set()) & context_types)
        stock_roots = sorted(root for root in clean_aligned if root_components.get(root, set()) & stock_types)
        legacy_groups = [set(group) for group in integration_policy.get("required_aligned_domain_groups", [])]
        group_coverage = (
            [sorted(aligned_domains.intersection(group)) for group in legacy_groups]
            if legacy_groups else [
                sorted(domain for domain in aligned_domains if domain in {"market", "relationships", "event"}),
                sorted(domain for domain in aligned_domains if domain in {"capital", "derivatives", "price_volume"}),
            ]
        )
        decision_features = {
            "domain_states": {key: relative_states[key] for key in sorted(relative_states)},
            "aligned_independent_root_count": len(clean_aligned),
            "opposed_independent_root_count": len(clean_opposed),
            "conflicted_lineage_root_count": len(conflicted),
            "aligned_applicable_domains": sorted(aligned_domains),
            "market_or_industry_root_ids": context_roots,
            "stock_specific_root_ids": stock_roots,
            "aligned_domain_group_coverage": group_coverage,
        }
        if decision_features["aligned_independent_root_count"] < minimum_aligned:
            raise RunError("published direction lacks the required independent evidence roots")
        if len(aligned_domains) < minimum_domains:
            raise RunError("published direction lacks two applicable domain reviews")
        if decision_features["opposed_independent_root_count"] > maximum_opposed:
            raise RunError("published direction has an unresolved independent opposing root")
        if modern_policy:
            if not context_roots or not stock_roots:
                raise RunError("published direction lacks both context and stock-specific evidence roots")
        else:
            if any(not aligned_domains.intersection(group) for group in legacy_groups):
                raise RunError("published direction lacks legacy domain-group coverage")
        normalized_predictions.append({
            "forecast_id": f"{run_input['run_id']}:{symbol}",
            "symbol": symbol,
            "direction": prediction["direction"],
            "track": prediction["track"],
            "industry_group": prediction["industry_group"].strip(),
            "score_eligible": mode == "canary",
            "decision_features": decision_features,
            "decision_signature": "sig_" + sha256_payload({
                "track": prediction["track"],
                **decision_features,
            })[:20],
            "strongest_countercase": normalized_adversary[symbol]["strongest_countercase"],
            "p_committee_hit": None,
            "p_net_profit": None,
        })

    output = {
        "schema_version": 6,
        "run_id": run_input["run_id"],
        "created_at": created_at,
        "cutoff_et": cutoff_et,
        "publication_deadline_et": publication_deadline_et,
        "formal_eligibility": formal_eligibility,
        "mode": mode,
        "prediction_target": "official_unadjusted_US_regular_session_open_to_close",
        "lineage": graph,
        "reports_by_symbol": normalized_reports,
        "adversary_by_symbol": normalized_adversary,
        "predictions": normalized_predictions,
        "p_committee_hit": None,
        "p_net_profit": None,
        "probability_publication_allowed": False,
        "integration_policy_sha256": sha256_payload(integration_policy),
        "candidate_intake_sha256": candidate_intake_sha256,
        "formal_ai_enabled": isolation["formal_ai_enabled"],
        "isolation_status_sha256": sha256_payload(isolation),
    }
    if "system_identity" in run_input or "system_config_sha256" in run_input:
        identity = str(run_input.get("system_identity", ""))
        config_sha = str(run_input.get("system_config_sha256", ""))
        if not identity or len(config_sha) != 64 or config_sha.lower() != config_sha:
            raise RunError("system identity or config hash is invalid")
        output["system_identity"] = identity
        output["system_config_sha256"] = config_sha
    if "integration_audit_by_symbol" in run_input:
        audit = run_input["integration_audit_by_symbol"]
        if not isinstance(audit, dict) or set(audit) != set(normalized_reports):
            raise RunError("integration audit does not match analyzed symbols")
        output["integration_audit_by_symbol"] = audit
    output["run_sha256"] = sha256_payload(output)
    return output
