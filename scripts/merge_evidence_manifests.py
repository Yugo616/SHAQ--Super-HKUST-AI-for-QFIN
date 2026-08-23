#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import merge_evidence_manifests  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge verified evidence into a snapshot manifest")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--event-record", type=Path, action="append", default=[])
    parser.add_argument("--additional-manifest", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("merged evidence manifest is immutable")
    records = [json.loads(path.read_text(encoding="utf-8")) for path in args.event_record]
    for path in args.additional_manifest:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document.get("evidence"), list):
            raise ValueError("additional manifest has no evidence records")
        records.extend(document["evidence"])
    result = merge_evidence_manifests(
        json.loads(args.base.read_text(encoding="utf-8")), records,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_count": len(result["evidence"]), "added_record_count": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
