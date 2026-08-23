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

from daily_oracle_v6 import (  # noqa: E402
    formal_ai_status,
    freeze_run,
    validate_isolation_status,
    verify_isolation_attestation,
)
from daily_oracle_v6.hashing import sha256_file, sha256_payload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and freeze one Daily Oracle V6 run")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--integration-policy", type=Path, default=ROOT / "config/integration.json")
    parser.add_argument("--integration-policy-snapshot", type=Path, required=True)
    parser.add_argument("--candidate-intake", type=Path, required=True)
    parser.add_argument("--isolation-status", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("a frozen V6 run is never overwritten")
    run_input = json.loads(args.input.read_text(encoding="utf-8"))
    run_input["created_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    integration_policy = json.loads(args.integration_policy.read_text(encoding="utf-8"))
    isolation_status = validate_isolation_status(
        json.loads(args.isolation_status.read_text(encoding="utf-8"))
    )
    if isolation_status["formal_ai_enabled"]:
        verify_isolation_attestation(
            status=isolation_status,
            attestation_path=args.isolation_status.with_name("isolation_attestation.json"),
            workspace_root=ROOT.parent,
        )
    elif sha256_payload(isolation_status) != sha256_payload(formal_ai_status()):
        raise ValueError("disabled isolation status was not produced by this installed public runner")
    if args.integration_policy_snapshot.exists():
        raise FileExistsError("integration policy snapshot is immutable")
    result = freeze_run(
        run_input=run_input,
        evidence_root=args.evidence_root,
        integration_policy=integration_policy,
        candidate_intake_sha256=sha256_file(args.candidate_intake),
        isolation_status=isolation_status,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.integration_policy_snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.integration_policy_snapshot.write_text(
        json.dumps(integration_policy, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"froze {result['run_id']} as {result['mode']}: {result['run_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
