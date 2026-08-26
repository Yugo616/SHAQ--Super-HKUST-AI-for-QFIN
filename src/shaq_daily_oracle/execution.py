from __future__ import annotations

import math
import hashlib
from datetime import datetime
from typing import Any

from .hashing import sha256_payload


class ExecutionError(ValueError):
    """Execution state is unsafe or non-idempotent."""


TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}

_FUTU_STATUS_MAP = {
    "WAITING_SUBMIT": "SUBMITTED",
    "SUBMITTING": "SUBMITTED",
    "SUBMITTED": "SUBMITTED",
    "FILLED_PART": "PARTIAL",
    "FILLED_ALL": "FILLED",
    "CANCELLING_PART": "PARTIAL",
    "CANCELLING_ALL": "SUBMITTED",
    "CANCELLED_PART": "CANCELLED",
    "CANCELLED_ALL": "CANCELLED",
    "FAILED": "REJECTED",
    "DISABLED": "REJECTED",
    "DELETED": "REJECTED",
}


def enforce_execution_window(now: datetime, policy: dict[str, Any], phase: str) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExecutionError("execution time requires an offset")
    field_pair = {
        "entry": ("entry_after", "entry_deadline"),
        "exit": ("exit_at", "exit_deadline"),
    }.get(phase)
    if field_pair is None:
        return
    try:
        start = datetime.fromisoformat(str(policy[field_pair[0]]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(policy[field_pair[1]]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"invalid {phase} submission window") from exc
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ExecutionError(f"invalid {phase} submission window")
    if not start <= now <= end:
        raise ExecutionError(f"{phase} is outside the immutable submission window")


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value)).split(".")[-1].upper()


def normalize_futu_order_status(value: Any) -> str:
    name = _enum_name(value)
    if name not in _FUTU_STATUS_MAP:
        raise ExecutionError(f"unsupported Futu order status: {name}")
    return _FUTU_STATUS_MAP[name]


def broker_remark(idempotency_key: str, phase: str) -> str:
    if not idempotency_key or phase not in {"entry", "exit"}:
        raise ExecutionError("broker remark requires an idempotency key and entry/exit phase")
    digest = hashlib.sha256(f"{idempotency_key}:{phase}".encode("utf-8")).hexdigest()
    return f"DO6:{phase[0].upper()}:{digest[:32]}"


def verify_execution_bundle(
    bundle: dict[str, Any], frozen: dict[str, Any], policy: dict[str, Any]
) -> None:
    bundle_unsigned = dict(bundle)
    declared_bundle_hash = bundle_unsigned.pop("intent_bundle_sha256", None)
    if declared_bundle_hash != sha256_payload(bundle_unsigned):
        raise ExecutionError("intent bundle hash mismatch")
    frozen_unsigned = dict(frozen)
    declared_run_hash = frozen_unsigned.pop("run_sha256", None)
    if declared_run_hash != sha256_payload(frozen_unsigned):
        raise ExecutionError("frozen run hash mismatch")
    if (
        bundle.get("frozen_run_sha256") != declared_run_hash
        or bundle.get("run_id") != frozen.get("run_id")
        or bundle.get("mode") != "canary"
        or frozen.get("mode") != "canary"
        or bundle.get("trd_env") != "SIMULATE"
    ):
        raise ExecutionError("intent bundle is not bound to an eligible frozen canary")
    if (
        bundle.get("execution_policy_sha256") != sha256_payload(policy)
        or policy.get("run_id") != frozen.get("run_id")
        or policy.get("trd_env") != "SIMULATE"
        or policy.get("real_trading_enabled") is not False
    ):
        raise ExecutionError("execution policy is not bound to the canary")
    for field in ("portfolio_snapshot_sha256", "borrowability_snapshot_sha256"):
        value = bundle.get(field)
        if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
            raise ExecutionError(f"{field} is invalid")
    forecasts = {row["symbol"]: row for row in frozen.get("predictions", [])}
    required = {
        "run_id", "symbol", "side", "quantity", "account_alias", "intent_id",
        "idempotency_key", "trd_env", "session", "order_type", "submit_after",
        "submit_before", "exit_at", "status",
    }
    seen = set()
    try:
        configured_quantity = int(policy["shares_per_forecast"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError("invalid configured forecast quantity") from exc
    if configured_quantity <= 0:
        raise ExecutionError("invalid configured forecast quantity")
    for intent in bundle.get("intents", []):
        if set(intent) != required:
            raise ExecutionError("intent keys differ from execution contract")
        symbol = intent["symbol"]
        if symbol in seen or symbol not in forecasts:
            raise ExecutionError("intent symbol is duplicated or absent from frozen forecasts")
        seen.add(symbol)
        expected_side = "BUY" if forecasts[symbol]["direction"] == "bullish" else "SELL_SHORT"
        if (
            intent["side"] != expected_side
            or intent["quantity"] != configured_quantity
            or intent["trd_env"] != "SIMULATE"
            or intent["session"] != "RTH"
            or intent["order_type"] != "MARKET"
            or intent["status"] != "READY"
            or intent["submit_after"] != policy.get("entry_after")
            or intent["submit_before"] != policy.get("entry_deadline")
            or intent["exit_at"] != policy.get("exit_at")
            or intent["account_alias"] not in set(policy.get("account_allowlist", []))
        ):
            raise ExecutionError("intent differs from the frozen paper canary")
        seed = {
            "run_id": intent["run_id"],
            "symbol": symbol,
            "side": intent["side"],
            "quantity": configured_quantity,
            "account_alias": intent["account_alias"],
        }
        if (
            intent["intent_id"] != "intent_" + sha256_payload(seed)[:20]
            or intent["idempotency_key"] != sha256_payload({"intent": seed, "version": 6})
        ):
            raise ExecutionError("intent identity does not match its immutable seed")


def broker_update_from_row(row: dict[str, Any]) -> dict[str, Any]:
    order_id = str(row.get("order_id", "")).strip()
    if not order_id:
        raise ExecutionError("broker order row is missing order_id")
    try:
        dealt_qty = float(row.get("dealt_qty", 0))
    except (TypeError, ValueError) as exc:
        raise ExecutionError("invalid broker dealt quantity") from exc
    if not math.isfinite(dealt_qty) or dealt_qty < 0:
        raise ExecutionError("invalid broker dealt quantity")
    average = row.get("dealt_avg_price")
    if average in (None, "", "N/A"):
        average = None
    else:
        try:
            average = float(average)
        except (TypeError, ValueError) as exc:
            raise ExecutionError("invalid broker average price") from exc
        if not math.isfinite(average) or average <= 0:
            average = None
    return {
        "trd_env": "SIMULATE",
        "status": normalize_futu_order_status(row.get("order_status")),
        "broker_order_id": order_id,
        "dealt_qty": dealt_qty,
        "dealt_avg_price": average,
        "reconciliation_status": "reconciled",
        "broker_updated_time": row.get("updated_time"),
    }


def find_broker_order(record: dict[str, Any], broker_rows: list[dict[str, Any]]) -> dict[str, Any]:
    order_id = str(record.get("broker_order_id") or "")
    remark = str(record.get("remark") or "")
    matches = [
        row
        for row in broker_rows
        if (order_id and str(row.get("order_id")) == order_id)
        or (remark and str(row.get("remark")) == remark)
    ]
    unique_ids = {str(row.get("order_id")) for row in matches}
    if not matches:
        raise ExecutionError("broker order could not be reconciled")
    if len(unique_ids) != 1:
        raise ExecutionError("broker order reconciliation is ambiguous")
    return matches[0]


def exit_quantity_from_entry(entry_record: dict[str, Any]) -> int:
    if entry_record.get("trd_env") != "SIMULATE":
        raise ExecutionError("entry environment mismatch")
    if entry_record.get("reconciliation_status") != "reconciled":
        raise ExecutionError("entry must be reconciled before exit")
    if entry_record.get("status") not in TERMINAL:
        raise ExecutionError("entry must be terminal before exit")
    quantity = float(entry_record.get("dealt_qty", 0))
    if quantity <= 0:
        return 0
    if not quantity.is_integer():
        raise ExecutionError("fractional stock fills are unsupported")
    return int(quantity)


def select_simulate_us_account(accounts: list[dict[str, Any]]) -> int:
    candidates = []
    for account in accounts:
        environment = _enum_name(account.get("trd_env"))
        role = _enum_name(account.get("acc_role"))
        authorization = account.get("trdmarket_auth") or []
        if isinstance(authorization, str):
            authorization = [authorization]
        markets = {_enum_name(value) for value in authorization}
        account_id = account.get("acc_id")
        if (
            environment == "SIMULATE"
            and role != "MASTER"
            and "US" in markets
            and str(account_id).isdigit()
        ):
            candidates.append(int(account_id))
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise ExecutionError("exactly one active US SIMULATE account is required")
    return unique[0]


def register_intent(intent: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    if intent.get("trd_env") != "SIMULATE":
        raise ExecutionError("only SIMULATE intents are accepted")
    key = intent.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise ExecutionError("idempotency_key is required")
    existing = journal.get(key)
    if existing:
        if existing.get("intent_id") != intent.get("intent_id"):
            raise ExecutionError("idempotency key collision")
        return existing
    record = {
        "intent_id": intent["intent_id"],
        "idempotency_key": key,
        "trd_env": "SIMULATE",
        "status": "REGISTERED",
        "broker_order_id": None,
        "dealt_qty": 0,
        "dealt_avg_price": None,
        "reconciliation_status": "awaiting_reconciliation",
    }
    journal[key] = record
    return record


def apply_broker_update(record: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    if update.get("trd_env") != "SIMULATE":
        raise ExecutionError("broker environment mismatch")
    if record["status"] in TERMINAL and update.get("status") != record["status"]:
        raise ExecutionError("terminal execution cannot change state")
    quantity = float(update.get("dealt_qty", 0))
    if quantity < float(record.get("dealt_qty", 0)):
        raise ExecutionError("dealt quantity cannot decrease")
    average = update.get("dealt_avg_price")
    if average is None:
        average = record.get("dealt_avg_price")
    merged = {
        **record,
        "status": update["status"],
        "broker_order_id": update.get("broker_order_id", record.get("broker_order_id")),
        "dealt_qty": quantity,
        "dealt_avg_price": average,
        "reconciliation_status": update.get(
            "reconciliation_status", "awaiting_reconciliation"
        ),
    }
    if "broker_updated_time" in update:
        merged["broker_updated_time"] = update["broker_updated_time"]
    return merged


def reconciled_journal_status(orders: dict[str, dict[str, Any]]) -> str:
    """Distinguish a fully terminal journal from one that still has live orders."""
    broker_records = [
        record
        for record in orders.values()
        if record.get("broker_order_id") or record.get("remark")
    ]
    if not broker_records:
        return "NO_TRADE"
    if any(
        record.get("reconciliation_status") not in {"reconciled", "local_terminal"}
        for record in broker_records
    ):
        return "RECONCILIATION_INCOMPLETE"
    if any(record.get("status") not in TERMINAL for record in broker_records):
        return "RECONCILED_ACTIVE"
    return "RECONCILED"


def phase_is_terminal(
    orders: dict[str, dict[str, Any]], idempotency_keys: list[str], phase: str
) -> bool:
    """Return true only when every recorded order for a phase is broker-terminal."""
    if phase not in {"entry", "exit"}:
        raise ExecutionError("phase must be entry or exit")
    if not idempotency_keys:
        return True
    for key in idempotency_keys:
        record = orders.get(f"{key}:{phase}")
        if record is None and phase == "exit":
            entry = orders.get(f"{key}:entry")
            if (
                entry is not None
                and entry.get("status") in TERMINAL
                and float(entry.get("dealt_qty", 0)) == 0
            ):
                continue
        if not record or not (
            record.get("status") in TERMINAL
            and record.get("reconciliation_status") in {"reconciled", "local_terminal"}
        ):
            return False
    return True
