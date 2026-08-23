#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6 import build_primary_event_record  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a captured primary source into event evidence")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--provider", default="issuer-primary")
    parser.add_argument("--analysis-view", type=Path)
    parser.add_argument("--analysis-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("primary event record is immutable")
    result = build_primary_event_record(
        symbol=args.symbol, raw_file=args.raw, receipt_file=args.receipt,
        evidence_root=args.evidence_root, provider=args.provider,
        analysis_file=args.analysis_view, analysis_receipt_file=args.analysis_receipt,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_id": result["evidence_id"], "symbol": result["scope_symbols"][0]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
