#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import build_canary_intents  # noqa: E402
from shaq_daily_oracle.hashing import sha256_file, sha256_payload  # noqa: E402


def read(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build safe SHAQ paper-canary intents")
    parser.add_argument("--forecast", type=Path, required=True)
    parser.add_argument("--portfolio", type=Path, required=True)
    parser.add_argument("--borrow", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=ROOT / "config/canary.example.json")
    parser.add_argument("--policy-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy_snapshot = args.policy_snapshot
    output = args.output
    if policy_snapshot.exists() or output.exists():
        raise FileExistsError("execution policy and intent bundle are immutable")
    forecast_document = read(args.forecast)
    unsigned_forecast = dict(forecast_document)
    declared_run_hash = unsigned_forecast.pop("run_sha256", None)
    if declared_run_hash != sha256_payload(unsigned_forecast):
        raise ValueError("frozen run hash mismatch")
    policy = read(args.policy)
    if policy.get("run_id") != forecast_document.get("run_id"):
        raise ValueError("policy and frozen forecast run_id differ")
    if policy.get("forecast_cutoff") != forecast_document.get(
        "publication_deadline_et", forecast_document.get("cutoff_et")
    ):
        raise ValueError("policy and frozen forecast cutoff differ")
    policy["created_at"] = forecast_document["created_at"]
    policy["forecast_mode"] = forecast_document["mode"]
    policy["intent_created_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    bindings = policy.get("parameter_bindings", {})
    for name in (
        "forecast_cutoff", "entry_after", "entry_deadline", "exit_at", "exit_deadline",
        "trd_env", "real_trading_enabled", "account_allowlist", "max_forecasts",
        "shares_per_forecast", "max_portfolio_age_seconds", "max_borrow_age_seconds",
    ):
        if len(bindings.get(name, [])) != 3:
            raise ValueError(f"{name} requires reference, decision and experiment bindings")
    borrow_document = read(args.borrow)
    portfolio_document = read(args.portfolio)
    if borrow_document.get("trd_env") != "SIMULATE":
        raise ValueError("borrowability snapshot is not SIMULATE")
    policy["portfolio_observed_at"] = portfolio_document["observed_at_et"]
    policy["borrow_captured_at"] = borrow_document["captured_at_et"]
    borrowable = borrow_document.get("borrowable", borrow_document)
    if not isinstance(borrowable, dict):
        raise ValueError("borrowability file has no symbol map")
    result = build_canary_intents(
        forecasts=forecast_document["predictions"],
        portfolio=portfolio_document,
        borrowable=borrowable,
        policy=policy,
    )
    result["frozen_run_sha256"] = declared_run_hash
    result["execution_policy_sha256"] = sha256_payload(policy)
    result["portfolio_snapshot_sha256"] = sha256_file(args.portfolio)
    result["borrowability_snapshot_sha256"] = sha256_file(args.borrow)
    result["intent_bundle_sha256"] = sha256_payload(result)
    policy_snapshot.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    policy_snapshot.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {result['mode']} intents: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
