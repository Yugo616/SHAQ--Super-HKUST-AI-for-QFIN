#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create immutable SHAQ scientific-label placeholders"
    )
    parser.add_argument("--forecast", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(args.forecast.read_text(encoding="utf-8"))
    rows = [
        {
            "forecast_id": row.get("forecast_id", f"{frozen['run_id']}:{row['symbol']}"),
            "symbol": row["symbol"],
            "direction": row["direction"],
            "session_scope": "US_regular_session",
            "price_basis": "official_unadjusted_OHLC",
            "official_open": None,
            "official_close": None,
            "official_label_status": "awaiting_official_label",
            "flat_outcome_policy": "neutral_and_wrong_for_bullish_or_bearish",
        }
        for row in frozen["predictions"]
    ]
    payload = {
        "schema_version": 6,
        "run_id": frozen["run_id"],
        "prediction_target": "official_unadjusted_US_regular_session_open_to_close",
        "labels": rows,
    }
    if args.output.exists():
        raise FileExistsError("label placeholder is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} label placeholders: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
