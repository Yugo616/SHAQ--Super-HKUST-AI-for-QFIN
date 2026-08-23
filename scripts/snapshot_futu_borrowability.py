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

from daily_oracle_v6.execution import select_simulate_us_account  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze Futu US paper-account short capacity")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    args = parser.parse_args()

    from futu import (  # type: ignore
        OpenQuoteContext,
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        Session,
        TrdEnv,
        TrdMarket,
    )

    codes = [symbol if symbol.startswith("US.") else f"US.{symbol}" for symbol in args.symbols]
    quote = OpenQuoteContext(host=args.host, port=args.port)
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=args.host, port=args.port)
    try:
        ret, accounts = trade.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"account query failed: {accounts}")
        account_id = select_simulate_us_account([row.to_dict() for _, row in accounts.iterrows()])
        prices = {}
        if codes:
            ret, snapshots = quote.get_market_snapshot(codes)
            if ret != RET_OK:
                raise RuntimeError(f"snapshot query failed: {snapshots}")
            prices = {str(row["code"]): float(row["last_price"]) for _, row in snapshots.iterrows()}
        records = []
        for code in codes:
            price = prices.get(code, 0.0)
            if price <= 0:
                records.append({"symbol": code.removeprefix("US."), "borrowable": False, "max_sell_short": None, "reason": "missing_price"})
                continue
            ret, capacity = trade.acctradinginfo_query(
                order_type=OrderType.NORMAL,
                code=code,
                price=price,
                trd_env=TrdEnv.SIMULATE,
                acc_id=account_id,
                session=Session.RTH,
            )
            if ret != RET_OK or capacity.empty:
                records.append({"symbol": code.removeprefix("US."), "borrowable": False, "max_sell_short": None, "reason": "capacity_unavailable"})
                continue
            maximum = float(capacity.iloc[0]["max_sell_short"])
            records.append({
                "symbol": code.removeprefix("US."),
                "borrowable": maximum >= 1,
                "max_sell_short": maximum,
                "reason": None if maximum >= 1 else "zero_short_capacity",
            })
        payload = {
            "schema_version": 6,
            "captured_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "trd_env": "SIMULATE",
            "records": sorted(records, key=lambda row: row["symbol"]),
            "borrowable": {row["symbol"]: row["borrowable"] for row in records},
        }
        if args.output.exists():
            raise FileExistsError("borrowability snapshot is immutable")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote borrowability snapshot: {args.output}")
    finally:
        quote.close()
        trade.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
