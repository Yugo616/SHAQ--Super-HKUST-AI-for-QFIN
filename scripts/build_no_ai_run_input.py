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

from daily_oracle_v6 import build_no_ai_run_input  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an auditable no-AI V6 run input")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--candidate-intake", type=Path, required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--isolation-status", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("run input is immutable")
    created_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    result = build_no_ai_run_input(
        run_id=args.run_id,
        created_at=created_at,
        cutoff_et=args.cutoff_et,
        candidate_intake=json.loads(args.candidate_intake.read_text(encoding="utf-8")),
        evidence_manifest=json.loads(args.evidence_manifest.read_text(encoding="utf-8")),
        isolation_status=json.loads(args.isolation_status.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": len(result["reports_by_symbol"]), "prediction_count": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

