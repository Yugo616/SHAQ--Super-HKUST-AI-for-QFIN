#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6.universe import derive_effective_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive a PIT universe from an immutable seed and official events")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.manifest.exists():
        raise FileExistsError("effective universe and manifest are immutable")
    event_document = json.loads(args.events.read_text(encoding="utf-8"))
    rows, manifest = derive_effective_universe(
        base_csv=args.base,
        source_path=args.source,
        receipt_path=args.receipt,
        events=event_document["events"],
        as_of=args.as_of,
    )
    fieldnames = [
        "instrument", "ticker", "company_name", "gics_sector", "gics_sub_industry",
        "cik_company_id", "stable_security_id", "listing_id", "source_role", "source_url",
        "observed_active_at_utc", "known_from_utc",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    manifest["events_path"] = str(args.events.resolve())
    from daily_oracle_v6.hashing import sha256_file
    manifest["events_sha256"] = sha256_file(args.events)
    manifest["output_path"] = str(args.output.resolve())
    manifest["output_sha256"] = sha256_file(args.output)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "count": len(rows),
        "applied_events": manifest["applied_events"],
        "output_sha256": manifest["output_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
