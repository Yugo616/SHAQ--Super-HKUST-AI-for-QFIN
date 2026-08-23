#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.labels import build_label_row, validate_label_capture_time  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture immutable unadjusted SHAQ open-to-close labels")
    parser.add_argument("--forecast", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--phase", choices=["provisional", "final"], required=True)
    parser.add_argument("--session-close-et", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    args = parser.parse_args()
    frozen = json.loads(args.forecast.read_text(encoding="utf-8"))
    started_at = datetime.now(ZoneInfo("America/New_York"))
    session_close = datetime.fromisoformat(args.session_close_et.replace("Z", "+00:00"))
    validate_label_capture_time(
        now=started_at, session_close=session_close, trade_date=args.trade_date, phase=args.phase
    )

    from futu import AuType, KLType, OpenQuoteContext, RET_OK, Session  # type: ignore

    quote = OpenQuoteContext(host=args.host, port=args.port)
    labels = []
    try:
        for forecast in frozen["predictions"]:
            symbol = forecast["symbol"]
            ret, frame, _ = quote.request_history_kline(
                f"US.{symbol}",
                start=args.trade_date,
                end=args.trade_date,
                ktype=KLType.K_DAY,
                autype=AuType.NONE,
                max_count=10,
                session=Session.RTH,
            )
            if ret != RET_OK:
                raise RuntimeError(f"unadjusted daily bar query failed for {symbol}: {frame}")
            labels.append(build_label_row(
                run_id=frozen["run_id"],
                symbol=symbol,
                direction=forecast["direction"],
                trade_date=args.trade_date,
                rows=[row.to_dict() for _, row in frame.iterrows()],
                phase=args.phase,
                adjustment="NONE",
            ))
    finally:
        quote.close()
    captured_at = datetime.now(ZoneInfo("America/New_York"))
    validate_label_capture_time(
        now=captured_at,
        session_close=session_close,
        trade_date=args.trade_date,
        phase=args.phase,
    )
    payload = {
        "schema_version": 6,
        "run_id": frozen["run_id"],
        "provider": "Futu OpenD",
        "captured_at_et": captured_at.isoformat(),
        "adjustment": "NONE",
        "session_scope": "US_regular_session",
        "official_label_status": args.phase,
        "trade_date": args.trade_date,
        "session_close_et": session_close.isoformat(),
        "labels": labels,
    }
    if args.output.exists():
        raise FileExistsError("label observation is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(labels)} {args.phase} labels: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
