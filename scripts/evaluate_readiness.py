#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import cost_model, net_profit_readiness, probability_readiness  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate prospective SHAQ probability and cost gates")
    parser.add_argument("--evaluations", type=Path, required=True)
    parser.add_argument("--round-trips", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=ROOT / "config/readiness.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    for section in ("probability", "cost", "net_profit"):
        bindings = policy[section].get("parameter_bindings", {})
        for name, value in policy[section].items():
            if name in {"reference_id", "decision_id", "experiment_id", "parameter_bindings"}:
                continue
            if len(bindings.get(name, [])) != 3:
                raise ValueError(f"{section}.{name} lacks reference/decision/experiment bindings")
    evaluation_document = json.loads(args.evaluations.read_text(encoding="utf-8"))
    round_trip_document = json.loads(args.round_trips.read_text(encoding="utf-8"))
    evaluations = (
        evaluation_document["evaluations"]
        if isinstance(evaluation_document, dict)
        else evaluation_document
    )
    round_trips = (
        round_trip_document["round_trips"]
        if isinstance(round_trip_document, dict)
        else round_trip_document
    )
    probability = probability_readiness(evaluations, policy["probability"])
    cost = cost_model(round_trips, policy["cost"])
    result = {
        "probability": probability,
        "cost": cost,
        "net_profit": net_profit_readiness(
            evaluations, round_trips, probability, cost, policy["net_profit"]
        ),
    }
    if args.output.exists():
        raise FileExistsError("readiness result is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote readiness result: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
