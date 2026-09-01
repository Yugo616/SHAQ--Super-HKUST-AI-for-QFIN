#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.reliability import certify_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify one immutable Daily Oracle release")
    parser.add_argument("--ai-config", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--repetitions", type=int)
    args = parser.parse_args()
    certificate = certify_release(
        package_root=ROOT,
        ai_config_path=args.ai_config.expanduser().resolve(),
        data_root=args.data_root.expanduser().resolve(),
        repetitions=(
            args.repetitions
            if args.repetitions is not None
            else int(json.loads((ROOT / "config/reliability.json").read_text(
                encoding="utf-8"
            ))["certification_repetitions"])
        ),
    )
    print(json.dumps({
        "status": certificate["status"],
        "certification_id": certificate["certification_id"],
        "certificate_sha256": certificate["certificate_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
