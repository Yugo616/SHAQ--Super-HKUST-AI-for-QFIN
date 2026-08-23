#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6 import build_sec_analysis_text, build_sec_view_receipt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic text view of selected SEC documents")
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/event-analysis.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.receipt.exists():
        raise FileExistsError("SEC analysis view and receipt are immutable")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for name in ("document_types", "maximum_output_bytes"):
        if len(config.get("parameter_bindings", {}).get(name, [])) != 3:
            raise ValueError(f"{name} requires reference, decision and experiment bindings")
    raw = args.raw.read_bytes()
    analysis = build_sec_analysis_text(
        raw,
        document_types=config["document_types"],
        maximum_output_bytes=int(config["maximum_output_bytes"]),
    )
    receipt = build_sec_view_receipt(
        raw=raw,
        analysis=analysis,
        document_types=config["document_types"],
        maximum_output_bytes=int(config["maximum_output_bytes"]),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(analysis)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"analysis_bytes": len(analysis), "analysis_sha256": receipt["analysis_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
