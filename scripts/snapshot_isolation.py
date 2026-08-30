#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import (  # noqa: E402
    attest_openai_responses,
    attest_sandboxed_codex,
    formal_ai_status,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the formal-AI isolation capability status")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--backend", choices=("disabled", "openai-responses", "codex-cli"), default="disabled",
        help="Enable only after the selected evidence-only boundary passes",
    )
    parser.add_argument("--workspace-root", type=Path, default=ROOT.parent)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "config/ai-backend.json")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("isolation status is immutable")
    if args.backend == "codex-cli":
        attestation_path = args.attestation or args.output.with_name("isolation_attestation.json")
        artifact = attest_sandboxed_codex(
            workspace_root=args.workspace_root, output=attestation_path
        )
        status = artifact["status"]
    elif args.backend == "openai-responses":
        attestation_path = args.attestation or args.output.with_name("isolation_attestation.json")
        artifact = attest_openai_responses(
            workspace_root=args.workspace_root,
            output=attestation_path,
            config=json.loads(args.config.read_text(encoding="utf-8")),
        )
        status = artifact["status"]
    else:
        if args.attestation:
            raise ValueError("disabled backend cannot write a formal attestation")
        status = formal_ai_status()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
