#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.tasks import build_blind_domain_tasks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build six blind SHAQ domain tasks per candidate")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--as-of-et", required=True)
    parser.add_argument("--horizon", default="official_US_regular_session_open_to_close")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lineage = json.loads(args.lineage.read_text(encoding="utf-8"))
    result = build_blind_domain_tasks(
        lineage=lineage,
        symbols=args.symbols,
        as_of_et=args.as_of_et,
        horizon=args.horizon,
    )
    if args.output.exists():
        raise FileExistsError("domain task bundle is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(result['tasks'])} blind domain tasks: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
