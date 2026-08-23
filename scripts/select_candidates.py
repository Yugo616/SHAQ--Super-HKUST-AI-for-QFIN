#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.candidates import select_candidates  # noqa: E402
from shaq_daily_oracle.hashing import sha256_file, sha256_payload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Select direction-blind SHAQ deep-review candidates")
    parser.add_argument("--stocks", type=Path, required=True)
    parser.add_argument("--benchmarks", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--benchmark-config", type=Path, default=ROOT / "config/market-benchmarks.csv")
    parser.add_argument("--policy", type=Path, default=ROOT / "config/candidate-intake.json")
    parser.add_argument("--policy-snapshot", type=Path, required=True)
    parser.add_argument("--event-symbol", action="append", default=[])
    parser.add_argument("--exclude-symbol", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.policy_snapshot.exists():
        raise FileExistsError("candidate intake and policy snapshot are immutable")
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    result = select_candidates(
        stock_snapshot=json.loads(args.stocks.read_text(encoding="utf-8")),
        benchmark_snapshot=json.loads(args.benchmarks.read_text(encoding="utf-8")),
        universe_csv=args.universe,
        benchmark_csv=args.benchmark_config,
        captured_event_symbols=args.event_symbol,
        excluded_symbols=args.exclude_symbol,
        policy=policy,
    )
    result["inputs"] = {
        "stock_snapshot_path": str(args.stocks.resolve()),
        "stock_snapshot_sha256": sha256_file(args.stocks),
        "benchmark_snapshot_path": str(args.benchmarks.resolve()),
        "benchmark_snapshot_sha256": sha256_file(args.benchmarks),
        "universe_path": str(args.universe.resolve()),
        "universe_sha256": sha256_file(args.universe),
        "benchmark_config_path": str(args.benchmark_config.resolve()),
        "benchmark_config_sha256": sha256_file(args.benchmark_config),
        "candidate_policy_sha256": sha256_payload(policy),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.policy_snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.policy_snapshot.write_text(
        json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": len(result["candidates"]), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
