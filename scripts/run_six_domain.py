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

from shaq_daily_oracle import run_six_domain  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run six blinded domains with the governed AI backend")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--candidate-intake", type=Path, required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--isolation-status", type=Path, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/ai-backend.json")
    parser.add_argument("--integration-policy", type=Path, default=ROOT / "config/integration.json")
    parser.add_argument("--calls-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("run input is immutable")
    result = run_six_domain(
        run_id=args.run_id,
        created_at=datetime.now(ZoneInfo("America/New_York")).isoformat(),
        cutoff_et=args.cutoff_et,
        tasks_document=json.loads(args.tasks.read_text(encoding="utf-8")),
        lineage=json.loads(args.lineage.read_text(encoding="utf-8")),
        candidate_intake=json.loads(args.candidate_intake.read_text(encoding="utf-8")),
        evidence_manifest=json.loads(args.evidence_manifest.read_text(encoding="utf-8")),
        evidence_root=args.evidence_root,
        package_root=ROOT,
        workspace_root=ROOT.parent,
        isolation_status=json.loads(args.isolation_status.read_text(encoding="utf-8")),
        attestation_path=args.attestation,
        config=json.loads(args.config.read_text(encoding="utf-8")),
        integration_policy=json.loads(args.integration_policy.read_text(encoding="utf-8")),
        calls_dir=args.calls_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "candidate_count": len(result["reports_by_symbol"]),
        "prediction_count": len(result["predictions"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
