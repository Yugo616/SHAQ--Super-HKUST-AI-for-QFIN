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
    parser = argparse.ArgumentParser(description="Freeze a de-identified Futu US paper portfolio")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account-alias", default="US_SIMULATE_CANARY")
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    args = parser.parse_args()

    from futu import OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket  # type: ignore

    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=args.host, port=args.port)
    try:
        ret, accounts = trade.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"account query failed: {accounts}")
        account_id = select_simulate_us_account([row.to_dict() for _, row in accounts.iterrows()])
        ret, funds = trade.accinfo_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
        )
        if ret != RET_OK or funds.empty:
            raise RuntimeError(f"funds query failed: {funds}")
        ret, positions = trade.position_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
        )
        if ret != RET_OK:
            raise RuntimeError(f"position query failed: {positions}")
        position_rows = []
        for _, row in positions.iterrows():
            quantity = float(row["qty"])
            if quantity == 0:
                continue
            position_rows.append({
                "symbol": str(row["code"]).removeprefix("US."),
                "quantity": quantity,
                "can_sell_quantity": float(row["can_sell_qty"]),
                "average_cost": float(row["average_cost"]),
                "origin": "external",
            })
        fund_row = funds.iloc[0]
        payload = {
            "schema_version": 6,
            "observed_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "account_alias": args.account_alias,
            "trd_env": "SIMULATE",
            "cash": float(fund_row["cash"]),
            "total_assets": float(fund_row["total_assets"]),
            "positions": sorted(position_rows, key=lambda row: row["symbol"]),
        }
        if args.output.exists():
            raise FileExistsError("portfolio snapshot is immutable")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote de-identified portfolio snapshot: {args.output}")
    finally:
        trade.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
