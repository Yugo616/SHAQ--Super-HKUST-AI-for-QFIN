#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import build_snapshot_evidence_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a verified Futu snapshot evidence manifest")
    parser.add_argument("--stocks", type=Path, required=True)
    parser.add_argument("--benchmarks", type=Path, required=True)
    parser.add_argument("--benchmark-config", type=Path, default=ROOT / "config/market-benchmarks.csv")
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("evidence manifest is immutable")
    with args.benchmark_config.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    channels = {
        str(row["instrument"]).strip().upper(): str(row["lineage_channel"]).strip()
        for row in rows
    }
    if len(channels) != len(rows) or "" in channels.values():
        raise ValueError("benchmark lineage channels are blank or duplicated")
    result = build_snapshot_evidence_manifest(
        stock_snapshot=json.loads(args.stocks.read_text(encoding="utf-8")),
        benchmark_snapshot=json.loads(args.benchmarks.read_text(encoding="utf-8")),
        evidence_root=args.evidence_root,
        benchmark_channels=channels,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_count": len(result["evidence"]), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
