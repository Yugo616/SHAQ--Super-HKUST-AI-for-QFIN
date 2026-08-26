#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.execution import (  # noqa: E402
    apply_broker_update,
    broker_update_from_row,
    broker_remark,
    enforce_execution_window,
    exit_quantity_from_entry,
    find_broker_order,
    register_intent,
    reconciled_journal_status,
    phase_is_terminal,
    select_simulate_us_account,
    verify_execution_bundle,
)


def _read(path: Path, default: dict | None = None) -> dict:
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit or close idempotent Futu paper-canary orders")
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--frozen-run", type=Path, required=True)
    parser.add_argument("--policy-snapshot", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--phase", choices=["entry", "exit", "reconcile"], required=True)
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()

    bundle = _read(args.intents)
    frozen = _read(args.frozen_run)
    policy = _read(args.policy_snapshot)
    verify_execution_bundle(bundle, frozen, policy)
    journal = _read(args.journal, {"schema_version": 6, "orders": {}})
    identity_keys = [intent["idempotency_key"] for intent in bundle["intents"]]
    if args.phase in {"entry", "exit"} and phase_is_terminal(
        journal["orders"], identity_keys, args.phase
    ):
        journal["run_status"] = "NO_TRADE" if not identity_keys else "RECONCILED"
        journal["last_error"] = None
        _write(args.journal, journal)
        print(f"paper {args.phase} already terminal; journal={args.journal}")
        return 0
    now = datetime.now(ZoneInfo("America/New_York"))
    enforce_execution_window(now, policy, args.phase)

    from futu import (  # type: ignore
        MarketState,
        ModifyOrderOp,
        OpenQuoteContext,
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        Session,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )

    quote = OpenQuoteContext(host=args.host, port=args.port)
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=args.host, port=args.port)
    try:
        account_text = os.environ.get("DAILY_ORACLE_FUTU_SIM_ACCOUNT_ID", "")
        ret, account_frame = trade.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"account query failed: {account_frame}")
        account_rows = [row.to_dict() for _, row in account_frame.iterrows()]
        account_id = select_simulate_us_account(account_rows)
        if account_text:
            if not account_text.isdigit() or int(account_text) != account_id:
                raise ValueError("configured account is not the uniquely verified US SIMULATE account")
        codes = [f"US.{intent['symbol']}" for intent in bundle["intents"]]
        latest_prices = {}
        arrival_quotes = {}
        if codes:
            ret, snapshots = quote.get_market_snapshot(codes)
            if ret != RET_OK:
                raise RuntimeError(f"snapshot query failed: {snapshots}")
            for _, row in snapshots.iterrows():
                code = str(row["code"])
                last = float(row["last_price"])
                bid = float(row["bid_price"])
                ask = float(row["ask_price"])
                if last > 0:
                    latest_prices[code] = last
                arrival_quotes[code] = {
                    "last": last if last > 0 else None,
                    "bid": bid if bid > 0 else None,
                    "ask": ask if ask > 0 else None,
                    "mid": (bid + ask) / 2.0 if bid > 0 and ask >= bid else None,
                }
        if args.phase in {"entry", "exit"} and codes:
            ret, states = quote.get_market_state(codes)
            if ret != RET_OK:
                raise RuntimeError(f"market-state query failed: {states}")
            allowed = {str(MarketState.MORNING), str(MarketState.AFTERNOON), "MORNING", "AFTERNOON"}
            if any(str(value) not in allowed for value in states["market_state"]):
                raise RuntimeError("regular session is not open")

        ret, broker_orders = trade.order_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
        )
        if ret != RET_OK:
            raise RuntimeError(f"order query failed: {broker_orders}")
        broker_rows = [row.to_dict() for _, row in broker_orders.iterrows()]

        if args.phase == "entry" and bundle["intents"]:
            ret, live_positions = trade.position_list_query(
                trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
            )
            if ret != RET_OK:
                raise RuntimeError(f"live position query failed: {live_positions}")
            held_symbols = {
                str(row["code"]).removeprefix("US.")
                for _, row in live_positions.iterrows()
                if float(row["qty"]) != 0
            }
            collisions = sorted(
                held_symbols & {intent["symbol"] for intent in bundle["intents"]}
            )
            if collisions:
                raise RuntimeError(
                    "fresh external-position collision blocks entry: " + ",".join(collisions)
                )

        def reconcile_record(key: str) -> dict:
            record = journal["orders"][key]
            row = find_broker_order(record, broker_rows)
            updated = apply_broker_update(record, broker_update_from_row(row))
            journal["orders"][key] = updated
            return updated

        if args.phase == "reconcile":
            for intent in bundle["intents"]:
                for suffix in ("entry", "exit"):
                    key = f"{intent['idempotency_key']}:{suffix}"
                    record = journal["orders"].get(key)
                    if not record or not (record.get("broker_order_id") or record.get("remark")):
                        continue
                    if record.get("reconciliation_status") == "local_terminal":
                        continue
                    reconcile_record(key)
            order_ids = sorted({
                str(record["broker_order_id"])
                for record in journal["orders"].values()
                if record.get("broker_order_id")
            })
            if order_ids:
                fee_ret, fee_frame = trade.order_fee_query(
                    order_id_list=order_ids,
                    trd_env=TrdEnv.SIMULATE,
                    acc_id=account_id,
                )
                fee_rows = (
                    {str(row["order_id"]): row.to_dict() for _, row in fee_frame.iterrows()}
                    if fee_ret == RET_OK
                    else {}
                )
                for record in journal["orders"].values():
                    order_id = str(record.get("broker_order_id") or "")
                    fee_row = fee_rows.get(order_id)
                    if not fee_row:
                        record["fee_amount"] = None
                        record["fee_status"] = "unavailable"
                        continue
                    try:
                        fee_amount = float(fee_row["fee_amount"])
                    except (TypeError, ValueError):
                        fee_amount = None
                    record["fee_amount"] = fee_amount
                    record["fee_details"] = fee_row.get("fee_details", [])
                    record["fee_status"] = "observed" if fee_amount is not None else "unavailable"
            journal["run_status"] = reconciled_journal_status(journal["orders"])
            journal["last_error"] = None
            _write(args.journal, journal)
            print(f"paper reconcile processed; submit={args.submit}; journal={args.journal}")
            return 0

        for intent in bundle["intents"]:
            phase_seed = f"{intent['idempotency_key']}:{args.phase}"
            requested_quantity = int(intent["quantity"])
            if args.phase == "exit":
                entry_key = f"{intent['idempotency_key']}:entry"
                entry_record = journal["orders"].get(entry_key)
                if not entry_record:
                    continue
                if entry_record.get("broker_order_id") or entry_record.get("remark"):
                    entry_record = reconcile_record(entry_key)
                if entry_record.get("status") not in {"FILLED", "CANCELLED", "REJECTED"}:
                    if not args.submit or not entry_record.get("broker_order_id"):
                        raise RuntimeError("entry is not terminal; exit is blocked")
                    ret, cancel_result = trade.modify_order(
                        ModifyOrderOp.CANCEL,
                        order_id=entry_record["broker_order_id"],
                        qty=0,
                        price=0,
                        trd_env=TrdEnv.SIMULATE,
                        acc_id=account_id,
                    )
                    if ret != RET_OK:
                        raise RuntimeError(f"entry cancellation failed: {cancel_result}")
                    ret, refreshed_orders = trade.order_list_query(
                        trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
                    )
                    if ret != RET_OK:
                        raise RuntimeError(f"order refresh after cancellation failed: {refreshed_orders}")
                    broker_rows = [row.to_dict() for _, row in refreshed_orders.iterrows()]
                    entry_record = reconcile_record(entry_key)
                requested_quantity = exit_quantity_from_entry(entry_record)
                if requested_quantity == 0:
                    continue
            phase_intent = {
                **intent,
                "intent_id": f"{intent['intent_id']}:{args.phase}",
                "idempotency_key": phase_seed,
                "trd_env": "SIMULATE",
            }
            record = register_intent(phase_intent, journal["orders"])
            if record["status"] != "REGISTERED":
                continue
            remark = broker_remark(intent["idempotency_key"], args.phase)
            record["remark"] = remark
            record["symbol"] = intent["symbol"]
            record["requested_qty"] = requested_quantity
            matching_rows = [row for row in broker_rows if str(row.get("remark")) == remark]
            if matching_rows:
                if len({str(row.get("order_id")) for row in matching_rows}) != 1:
                    raise RuntimeError("broker idempotency remark is ambiguous")
                record["broker_order_id"] = str(matching_rows[0]["order_id"])
                journal["orders"][phase_seed] = apply_broker_update(
                    record, broker_update_from_row(matching_rows[0])
                )
                continue
            entry_side = intent["side"]
            side = entry_side if args.phase == "entry" else (
                "SELL" if entry_side == "BUY" else "BUY_BACK"
            )
            record["requested_side"] = side
            code = f"US.{intent['symbol']}"
            quote_record = arrival_quotes.get(code, {})
            record["arrival_last"] = quote_record.get("last")
            record["arrival_bid"] = quote_record.get("bid")
            record["arrival_ask"] = quote_record.get("ask")
            record["arrival_mid"] = quote_record.get("mid")
            if side == "SELL_SHORT":
                if code not in latest_prices:
                    record["status"] = "REJECTED"
                    record["rejection_reason"] = "missing_price_for_borrowability_check"
                    record["reconciliation_status"] = "local_terminal"
                    continue
                ret, capacity = trade.acctradinginfo_query(
                    order_type=OrderType.NORMAL,
                    code=code,
                    price=latest_prices[code],
                    trd_env=TrdEnv.SIMULATE,
                    acc_id=account_id,
                    session=Session.RTH,
                )
                if ret != RET_OK or capacity.empty or float(capacity.iloc[0]["max_sell_short"]) < requested_quantity:
                    record["status"] = "REJECTED"
                    record["rejection_reason"] = "futu_short_capacity_not_confirmed"
                    record["reconciliation_status"] = "local_terminal"
                    continue
            if not args.submit:
                record["status"] = "DRY_RUN"
                continue
            side_map = {
                "BUY": TrdSide.BUY,
                "SELL": TrdSide.SELL,
                "SELL_SHORT": TrdSide.SELL_SHORT,
                "BUY_BACK": TrdSide.BUY_BACK,
            }
            futu_side = side_map[side]
            ret, result = trade.place_order(
                price=0.0,
                qty=requested_quantity,
                code=code,
                trd_side=futu_side,
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                acc_id=account_id,
                remark=remark,
                session=Session.RTH,
            )
            if ret != RET_OK:
                record["status"] = "REJECTED"
                record["rejection_reason"] = str(result)
                record["reconciliation_status"] = "local_terminal"
                continue
            row = result.iloc[0]
            record["status"] = "SUBMITTED"
            record["broker_order_id"] = str(row["order_id"])
            record["arrival_price"] = quote_record.get("mid") or quote_record.get("last")
        if args.submit and args.phase in {"entry", "exit"}:
            interval = int(policy.get("order_poll_interval_seconds", 15))
            if not 5 <= interval <= 30:
                raise RuntimeError("invalid order polling interval")
            deadline = datetime.fromisoformat(
                str(policy[f"{args.phase}_deadline"]).replace("Z", "+00:00")
            )
            while datetime.now(ZoneInfo("America/New_York")) < deadline:
                if phase_is_terminal(journal["orders"], identity_keys, args.phase):
                    break
                time.sleep(interval)
                ret, refreshed_orders = trade.order_list_query(
                    trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
                )
                if ret != RET_OK:
                    raise RuntimeError(f"order polling failed: {refreshed_orders}")
                broker_rows = [row.to_dict() for _, row in refreshed_orders.iterrows()]
                for intent in bundle["intents"]:
                    key = f"{intent['idempotency_key']}:{args.phase}"
                    record = journal["orders"].get(key)
                    if not record or record.get("status") in {"FILLED", "CANCELLED", "REJECTED"}:
                        continue
                    if record.get("broker_order_id") or record.get("remark"):
                        reconcile_record(key)
                _write(args.journal, journal)
            for intent in bundle["intents"]:
                key = f"{intent['idempotency_key']}:{args.phase}"
                record = journal["orders"].get(key)
                if not record or record.get("status") in {"FILLED", "CANCELLED", "REJECTED"}:
                    continue
                if not record.get("broker_order_id"):
                    continue
                ret, cancel_result = trade.modify_order(
                    ModifyOrderOp.CANCEL,
                    order_id=record["broker_order_id"], qty=0, price=0,
                    trd_env=TrdEnv.SIMULATE, acc_id=account_id,
                )
                if ret != RET_OK:
                    raise RuntimeError(f"deadline cancellation failed: {cancel_result}")
            if not phase_is_terminal(journal["orders"], identity_keys, args.phase):
                ret, refreshed_orders = trade.order_list_query(
                    trd_env=TrdEnv.SIMULATE, acc_id=account_id, refresh_cache=True
                )
                if ret != RET_OK:
                    raise RuntimeError(f"final order refresh failed: {refreshed_orders}")
                broker_rows = [row.to_dict() for _, row in refreshed_orders.iterrows()]
                for intent in bundle["intents"]:
                    key = f"{intent['idempotency_key']}:{args.phase}"
                    if key in journal["orders"] and journal["orders"][key].get("broker_order_id"):
                        reconcile_record(key)
        journal["run_status"] = "PROCESSED"
        journal["last_error"] = None
        _write(args.journal, journal)
    except Exception as exc:
        journal["run_status"] = "BLOCKED"
        journal["last_error"] = {
            "phase": args.phase,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        _write(args.journal, journal)
        raise
    finally:
        quote.close()
        trade.close()
    print(f"paper {args.phase} processed; submit={args.submit}; journal={args.journal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
