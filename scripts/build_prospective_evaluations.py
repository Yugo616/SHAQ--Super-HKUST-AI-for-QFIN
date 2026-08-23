#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import build_prospective_evaluations  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Join frozen SHAQ forecasts to scientific labels")
    parser.add_argument("--forecast", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("prospective evaluation ledger is immutable")
    frozen = json.loads(args.forecast.read_text(encoding="utf-8"))
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    rows = build_prospective_evaluations(frozen, labels)
    payload = {
        "schema_version": 6,
        "run_id": frozen["run_id"],
        "official_label_status": labels["official_label_status"],
        "evaluations": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} prospective evaluations: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
