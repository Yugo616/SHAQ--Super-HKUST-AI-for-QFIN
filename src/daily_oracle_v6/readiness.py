from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean
from typing import Any, Iterable


class ReadinessError(ValueError):
    """A prospective probability or cost record is malformed."""


def build_prospective_evaluations(
    frozen: dict[str, Any], labels: dict[str, Any]
) -> list[dict[str, Any]]:
    if frozen.get("run_id") != labels.get("run_id"):
        raise ReadinessError("forecast and label run_id differ")
    by_id = {row["forecast_id"]: row for row in labels.get("labels", [])}
    if len(by_id) != len(labels.get("labels", [])):
        raise ReadinessError("duplicate label forecast_id")
    output = []
    for forecast in frozen.get("predictions", []):
        forecast_id = forecast["forecast_id"]
        label = by_id.get(forecast_id)
        if label is None:
            raise ReadinessError("forecast lacks a matching scientific label")
        output.append({
            "forecast_id": forecast_id,
            "symbol": forecast["symbol"],
            "trade_date": label["trade_date"],
            "track": forecast["track"],
            "direction": forecast["direction"],
            "industry_group": forecast["industry_group"],
            "score_eligible": bool(forecast["score_eligible"] and frozen.get("mode") == "canary"),
            "decision_signature": forecast["decision_signature"],
            "correct": label["correct"],
            "official_label_status": label["official_label_status"],
            "official_open_to_close_return": label["official_open_to_close_return"],
            "signed_forecast_return": (
                label["official_open_to_close_return"]
                if forecast["direction"] == "bullish"
                else -label["official_open_to_close_return"]
            ),
            "p_committee_hit": None,
            "p_net_profit": None,
        })
    if set(by_id) != {row["forecast_id"] for row in output}:
        raise ReadinessError("labels contain an unknown forecast_id")
    return output


def _prequential_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (str(row["trade_date"]), str(row["forecast_id"])))
    global_wins = global_total = 0
    signature_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    output = []
    dates = sorted({str(row["trade_date"]) for row in ordered})
    for trade_date in dates:
        same_day = [row for row in ordered if str(row["trade_date"]) == trade_date]
        for row in same_day:
            signature = str(row.get("decision_signature") or "")
            if not signature:
                raise ReadinessError("prospective evaluation lacks decision_signature")
            signature_wins, signature_total = signature_counts[signature]
            baseline = (global_wins + 0.5) / (global_total + 1.0)
            model = (signature_wins + 0.5) / (signature_total + 1.0)
            output.append({
                **row,
                "research_prequential_probability": model,
                "rolling_baseline_probability": baseline,
                "prior_signature_observations": signature_total,
                "prior_all_observations": global_total,
            })
        for row in same_day:
            signature = str(row["decision_signature"])
            won = 1 if row.get("correct") is True else 0
            signature_counts[signature][0] += won
            signature_counts[signature][1] += 1
            global_wins += won
            global_total += 1
    return output


def _loss(probability: float, outcome: float, kind: str) -> float:
    if kind == "brier":
        return (probability - outcome) ** 2
    clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
    return -(outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped))


def _paired_day_block_interval(
    rows: list[dict[str, Any]], *, kind: str, block_days: int, repetitions: int, seed: int
) -> dict[str, Any]:
    by_date: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        outcome = 1.0 if row.get("correct") is True else 0.0
        difference = _loss(float(row["research_prequential_probability"]), outcome, kind) - _loss(
            float(row["rolling_baseline_probability"]), outcome, kind
        )
        by_date[str(row["trade_date"])].append(difference)
    dates = sorted(by_date)
    daily = [mean(by_date[date]) for date in dates]
    if not daily:
        return {"mean_difference": None, "interval": None}
    block = min(block_days, len(daily))
    generator = random.Random(seed)
    draws = []
    for _ in range(repetitions):
        sample = []
        while len(sample) < len(daily):
            start = generator.randrange(len(daily))
            sample.extend(daily[(start + offset) % len(daily)] for offset in range(block))
        draws.append(mean(sample[:len(daily)]))
    draws.sort()
    lower_index = int(0.025 * (len(draws) - 1))
    upper_index = int(0.975 * (len(draws) - 1))
    return {
        "mean_difference": mean(daily),
        "interval": [draws[lower_index], draws[upper_index]],
        "orientation": "negative_favors_signature_model",
    }


def _risk_coverage(rows: list[dict[str, Any]], fractions: list[float]) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (
            -abs(float(row["research_prequential_probability"]) - 0.5),
            str(row["trade_date"]),
            str(row["forecast_id"]),
        ),
    )
    curve = []
    for fraction in fractions:
        count = max(1, math.ceil(len(ranked) * fraction))
        selected = ranked[:count]
        curve.append({
            "coverage": count / len(ranked),
            "count": count,
            "error_rate": mean(0.0 if row.get("correct") is True else 1.0 for row in selected),
        })
    return curve


def _day_block_mean_interval(
    rows: list[dict[str, Any]], *, value_field: str, block_days: int, repetitions: int, seed: int
) -> dict[str, Any]:
    by_date: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_date[str(row["trade_date"])].append(float(row[value_field]))
    dates = sorted(by_date)
    if not dates:
        return {"mean": None, "interval": None}
    daily = [mean(by_date[trade_date]) for trade_date in dates]
    block = min(block_days, len(daily))
    generator = random.Random(seed)
    draws = []
    for _ in range(repetitions):
        sample = []
        while len(sample) < len(daily):
            start = generator.randrange(len(daily))
            sample.extend(daily[(start + offset) % len(daily)] for offset in range(block))
        draws.append(mean(sample[:len(daily)]))
    draws.sort()
    return {
        "mean": mean(daily),
        "interval": [
            draws[int(0.025 * (len(draws) - 1))],
            draws[int(0.975 * (len(draws) - 1))],
        ],
    }


def _payoff_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if any(row.get("signed_forecast_return") is None for row in rows):
        return {
            "observations": 0,
            "payoff_fields_complete": False,
            "empirical_hit_rate": None,
            "conditional_gain_mean": None,
            "conditional_loss_mean": None,
            "gain_loss_ratio": None,
            "gross_expected_return": None,
        }
    signed = [float(row["signed_forecast_return"]) for row in rows]
    gains = [value for value in signed if value > 0]
    losses = [-value for value in signed if value <= 0]
    gain = mean(gains) if gains else None
    loss = mean(losses) if losses else None
    return {
        "observations": len(rows),
        "payoff_fields_complete": True,
        "empirical_hit_rate": mean(1.0 if row.get("correct") is True else 0.0 for row in rows) if rows else None,
        "conditional_gain_mean": gain,
        "conditional_loss_mean": loss,
        "gain_loss_ratio": gain / loss if gain is not None and loss not in {None, 0.0} else None,
        "gross_expected_return": mean(signed) if signed else None,
    }


def probability_readiness(
    evaluations: Iterable[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any]:
    rows = [
        row
        for row in evaluations
        if row.get("track") == "ordinary"
        and row.get("score_eligible") is True
        and row.get("official_label_status") == "final"
    ]
    forecast_ids = [str(row.get("forecast_id", "")) for row in rows]
    if "" in forecast_ids or len(forecast_ids) != len(set(forecast_ids)):
        raise ReadinessError("eligible probability records have blank or duplicate forecast_id")
    for row in rows:
        if not isinstance(row.get("correct"), bool):
            raise ReadinessError("a final eligible probability record lacks a boolean outcome")
        if not str(row.get("trade_date", "")).strip():
            raise ReadinessError("an eligible probability record lacks trade_date")
    dates = {str(row["trade_date"]) for row in rows}
    minimum_days = int(policy["minimum_trading_days"])
    minimum_forecasts = int(policy["minimum_evaluated_forecasts"])
    if minimum_days <= 0 or minimum_forecasts <= 0:
        raise ReadinessError("probability sample gates must be positive")
    sample_gate = len(dates) >= minimum_days and len(rows) >= minimum_forecasts
    prequential = _prequential_rows(rows) if rows else []
    payoff = _payoff_summary(rows)
    brier = None
    log_loss = None
    brier_comparison = log_comparison = None
    curve = []
    publication = False
    if sample_gate:
        probabilities = [float(row["research_prequential_probability"]) for row in prequential]
        outcomes = [1.0 if row.get("correct") is True else 0.0 for row in prequential]
        brier = mean((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes, strict=True))
        log_loss = -mean(
            outcome * math.log(probability) + (1.0 - outcome) * math.log(1.0 - probability)
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        )
        block_days = int(policy["bootstrap_block_days"])
        repetitions = int(policy["bootstrap_repetitions"])
        fractions = [float(value) for value in policy["risk_coverage_fractions"]]
        if block_days <= 0 or repetitions <= 0:
            raise ReadinessError("bootstrap settings must be positive")
        if fractions != sorted(set(fractions)) or not fractions or fractions[-1] != 1.0 or fractions[0] <= 0:
            raise ReadinessError("risk coverage fractions must be unique, increasing and end at one")
        comparison_args = {
            "block_days": block_days,
            "repetitions": repetitions,
            "seed": int(policy["bootstrap_seed"]),
        }
        brier_comparison = _paired_day_block_interval(prequential, kind="brier", **comparison_args)
        log_comparison = _paired_day_block_interval(prequential, kind="log", **comparison_args)
        curve = _risk_coverage(prequential, fractions)
        monotone = all(
            curve[index]["error_rate"] <= curve[index + 1]["error_rate"]
            for index in range(len(curve) - 1)
        )
        publication = (
            brier_comparison["interval"][1] < 0
            and log_comparison["interval"][1] < 0
            and monotone
        )
    return {
        "trading_days": len(dates),
        "evaluated_forecasts": len(rows),
        "sample_gate_passed": sample_gate,
        "proper_scores_available": brier is not None,
        "brier": brier,
        "log_loss": log_loss,
        "paired_block_bootstrap": {"brier": brier_comparison, "log_loss": log_comparison},
        "risk_coverage": curve,
        "probability_publication_allowed": publication,
        "p_committee_hit": None,
        "research_prequential_rows": prequential,
        "historical_payoff_distribution": payoff,
        "required_next_test": "publication_gate_passed" if publication else "continue_prospective_collection_or_fail_gate",
    }


def net_profit_readiness(
    evaluations: Iterable[dict[str, Any]],
    round_trips: Iterable[dict[str, Any]],
    probability_state: dict[str, Any],
    cost_state: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    eligible_rows = [
        row for row in evaluations
        if row.get("track") == "ordinary"
        and row.get("score_eligible") is True
        and row.get("official_label_status") == "final"
    ]
    eligible_ids = [str(row.get("forecast_id", "")) for row in eligible_rows]
    if "" in eligible_ids or len(eligible_ids) != len(set(eligible_ids)):
        raise ReadinessError("eligible net-profit records have blank or duplicate forecast_id")
    eligible = {row["forecast_id"]: row for row in eligible_rows}
    joined = []
    for trip in round_trips:
        evaluation = eligible.get(trip.get("forecast_id"))
        if (
            evaluation is None
            or trip.get("reconciliation_status") != "reconciled"
            or trip.get("outcome_status") != "round_trip_reconciled"
            or trip.get("net_fill_return") is None
        ):
            continue
        joined.append({
            **trip,
            "industry_group": evaluation["industry_group"],
            "net_realized_return": float(trip["net_fill_return"]),
        })
    dates = {str(row["trade_date"]) for row in joined}
    sample_gate = (
        len(dates) >= int(policy["minimum_trading_days"])
        and len(joined) >= int(policy["minimum_round_trips"])
    )
    interval = None
    concentration = None
    publication = False
    modeled_net_ev = None
    modeled_components = None
    payoff = probability_state.get("historical_payoff_distribution", {})
    probability = payoff.get("empirical_hit_rate")
    gain = payoff.get("conditional_gain_mean")
    loss = payoff.get("conditional_loss_mean")
    estimated_cost = cost_state.get("estimated_cost_return")
    if (
        probability_state.get("probability_publication_allowed") is True
        and cost_state.get("status") == "operational_paper_cost_model"
        and None not in {probability, gain, loss, estimated_cost}
    ):
        modeled_net_ev = (
            float(probability) * float(gain)
            - (1.0 - float(probability)) * float(loss)
            - float(estimated_cost)
        )
        modeled_components = {
            "p": probability,
            "conditional_gain": gain,
            "conditional_loss": loss,
            "cost": estimated_cost,
            "formula": "p*G-(1-p)*L-cost",
        }
    if sample_gate:
        block_days = int(policy["bootstrap_block_days"])
        repetitions = int(policy["bootstrap_repetitions"])
        if block_days <= 0 or repetitions <= 0:
            raise ReadinessError("net-EV bootstrap settings must be positive")
        interval = _day_block_mean_interval(
            joined,
            value_field="net_realized_return",
            block_days=block_days,
            repetitions=repetitions,
            seed=int(policy["bootstrap_seed"]),
        )
        leave_one_out = {}
        concentration_passed = True
        for field in ("symbol", "industry_group", "trade_date"):
            groups = sorted({str(row[field]) for row in joined})
            excluded_means = [
                mean(row["net_realized_return"] for row in joined if str(row[field]) != group)
                for group in groups
                if any(str(row[field]) != group for row in joined)
            ]
            minimum = min(excluded_means) if excluded_means and len(groups) >= 2 else None
            leave_one_out[field] = {"group_count": len(groups), "minimum_exclusion_mean": minimum}
            concentration_passed = concentration_passed and minimum is not None and minimum > 0
        concentration = {
            "method": "leave_one_symbol_industry_and_date_out",
            "groups": leave_one_out,
            "passed": concentration_passed,
        }
        publication = (
            probability_state.get("probability_publication_allowed") is True
            and cost_state.get("status") == "operational_paper_cost_model"
            and interval["interval"][0] > 0
            and concentration_passed
        )
    return {
        "status": "operational_net_profit_model" if publication else "collecting_prospective_net_returns",
        "trading_days": len(dates),
        "round_trips": len(joined),
        "sample_gate_passed": sample_gate,
        "modeled_net_expected_return": modeled_net_ev,
        "modeled_components": modeled_components,
        "actual_net_return_block_interval": interval,
        "concentration": concentration,
        "net_profit_publication_allowed": publication,
        "p_net_profit": None,
    }


def cost_model(
    round_trips: Iterable[dict[str, Any]], policy: dict[str, int]
) -> dict[str, Any]:
    rows = [
        row for row in round_trips
        if row.get("reconciliation_status") == "reconciled"
        and row.get("outcome_status") == "round_trip_reconciled"
    ]
    dates = {str(row["trade_date"]) for row in rows}
    minimum_days = int(policy["minimum_trading_days"])
    minimum_round_trips = int(policy["minimum_round_trips"])
    if minimum_days <= 0 or minimum_round_trips <= 0:
        raise ReadinessError("cost sample gates must be positive")
    ready = len(dates) >= minimum_days and len(rows) >= minimum_round_trips
    if not ready:
        return {
            "status": "collecting_prospective_fills",
            "trading_days": len(dates),
            "round_trips": len(rows),
            "estimated_cost_return": None,
            "components": None,
        }
    required = ("spread_return", "slippage_return", "fee_return", "borrow_return", "impact_return")
    for row in rows:
        if any(row.get(field) is None for field in required):
            raise ReadinessError("reconciled round trip lacks a cost component")
        values = {field: float(row[field]) for field in required}
        if any(not math.isfinite(value) for value in values.values()):
            raise ReadinessError("reconciled round trip has a non-finite cost component")
        if any(values[field] < 0 for field in ("spread_return", "fee_return", "borrow_return", "impact_return")):
            raise ReadinessError("a nonnegative cost component is negative")
    components = {field: mean(float(row[field]) for row in rows) for field in required}
    return {
        "status": "operational_paper_cost_model",
        "trading_days": len(dates),
        "round_trips": len(rows),
        "estimated_cost_return": sum(components.values()),
        "components": components,
        "real_market_impact_validated": False,
    }
