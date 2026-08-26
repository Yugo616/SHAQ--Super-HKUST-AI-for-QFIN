#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.audit import audit_runtime  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a SHAQ Daily Oracle canary directory")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=["preflight", "complete", "shadow_complete"], required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = audit_runtime(args.runtime, args.stage)
    if args.output.exists():
        raise FileExistsError("canary audit is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    import json
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{args.stage} canary audit passed: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
