#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6.ledger import evaluation_record, execution_cost_components  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an immutable fill-to-fill V6 paper ledger")
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = json.loads(args.intents.read_text(encoding="utf-8"))
    journal = json.loads(args.journal.read_text(encoding="utf-8"))
    rows = []
    for intent in bundle["intents"]:
        entry = journal.get("orders", {}).get(f"{intent['idempotency_key']}:entry", {})
        exit_order = journal.get("orders", {}).get(f"{intent['idempotency_key']}:exit", {})
        entry_qty = float(entry.get("dealt_qty", 0))
        exit_qty = float(exit_order.get("dealt_qty", 0))
        reconciled = (
            entry.get("reconciliation_status") == "reconciled"
            and exit_order.get("reconciliation_status") == "reconciled"
            and entry_qty > 0
            and entry_qty == exit_qty
        )
        no_entry_fill_terminal = (
            entry.get("reconciliation_status") in {"reconciled", "local_terminal"}
            and entry.get("status") in {"CANCELLED", "REJECTED"}
            and entry_qty == 0
            and not exit_order
        )
        outcome_status = (
            "round_trip_reconciled"
            if reconciled
            else ("no_entry_fill_terminal" if no_entry_fill_terminal else "incomplete")
        )
        entry_fee = entry.get("fee_amount")
        exit_fee = exit_order.get("fee_amount")
        fees = (
            float(entry_fee) + float(exit_fee)
            if reconciled and entry_fee is not None and exit_fee is not None
            else None
        )
        quote_complete = all(
            value is not None
            for value in (
                entry.get("arrival_bid"), entry.get("arrival_ask"),
                exit_order.get("arrival_bid"), exit_order.get("arrival_ask"),
            )
        )
        costs = (
            execution_cost_components(
                direction="bullish" if intent["side"] == "BUY" else "bearish",
                quantity=entry_qty,
                entry_fill=float(entry["dealt_avg_price"]),
                exit_fill=float(exit_order["dealt_avg_price"]),
                entry_bid=float(entry["arrival_bid"]),
                entry_ask=float(entry["arrival_ask"]),
                exit_bid=float(exit_order["arrival_bid"]),
                exit_ask=float(exit_order["arrival_ask"]),
                fees=fees,
            )
            if reconciled and fees is not None and quote_complete
            else {
                "spread_return": None,
                "slippage_return": None,
                "fee_return": None,
                "borrow_return": None,
                "impact_return": None,
                "implementation_shortfall_return": None,
                "borrow_separately_identified": False,
                "impact_separately_identified": False,
                "paper_cost_interpretation": "incomplete_arrival_quotes_fills_or_fees",
            }
        )
        evaluation = evaluation_record(
            forecast_id=f"{bundle['run_id']}:{intent['symbol']}",
            direction="bullish" if intent["side"] == "BUY" else "bearish",
            official_open=None,
            official_close=None,
            entry_fill=entry.get("dealt_avg_price") if reconciled else None,
            exit_fill=exit_order.get("dealt_avg_price") if reconciled else None,
            arrival_price=entry.get("arrival_price") if reconciled else None,
            fees=fees,
        )
        net_fill_return = (
            evaluation["actual_fill_return"] - costs["fee_return"]
            if evaluation["actual_fill_return"] is not None and costs["fee_return"] is not None
            else None
        )
        rows.append({
            "symbol": intent["symbol"],
            "trade_date": str(intent["submit_after"])[:10],
            "trd_env": "SIMULATE",
            "entry_status": entry.get("status", "missing"),
            "exit_status": exit_order.get("status", "missing"),
            "entry_dealt_qty": entry_qty,
            "exit_dealt_qty": exit_qty,
            "reconciliation_status": "reconciled" if reconciled else "incomplete",
            "outcome_status": outcome_status,
            "fee_status": (
                "observed"
                if fees is not None
                else ("unavailable" if reconciled else "not_applicable")
            ),
            "net_fill_return": net_fill_return,
            **costs,
            **evaluation,
        })
    payload = {
        "schema_version": 6,
        "run_id": bundle["run_id"],
        "trd_env": "SIMULATE",
        "scientific_labels_are_separate": True,
        "round_trips": rows,
    }
    if args.output.exists():
        raise FileExistsError("execution ledger is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} paper round trips: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
