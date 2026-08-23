from __future__ import annotations

import math
from typing import Any


class PremarketSemanticError(ValueError):
    """A provider snapshot cannot support a cutoff-safe premarket return."""


def build_symbol_snapshot_documents(
    *,
    provider: str,
    captured_at_start_et: str,
    captured_at_end_et: str,
    cutoff_et: str,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Create exact per-symbol documents so batching does not collapse lineage."""
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if not symbol or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
            for character in symbol
        ):
            raise PremarketSemanticError(f"unsafe canonical symbol: {symbol!r}")
        if symbol in documents:
            raise PremarketSemanticError(f"duplicate canonical symbol: {symbol}")
        documents[symbol] = {
            "schema_version": 6,
            "provider": provider,
            "captured_at_start_et": captured_at_start_et,
            "captured_at_end_et": captured_at_end_et,
            "cutoff_et": cutoff_et,
            **row,
        }
    return documents


def _finite(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def resolve_premarket_return(record: dict[str, Any], *, tolerance: float) -> dict[str, Any]:
    if not 0 < tolerance < 0.01:
        raise PremarketSemanticError("tolerance must be a return in (0, 0.01)")
    pre_price = _finite(record.get("pre_price"))
    if pre_price <= 0 or not math.isfinite(pre_price):
        return {"status": "no_premarket_trade", "reason": "pre_price_unavailable"}
    change_value = _finite(record.get("pre_change_val"))
    change_percent = _finite(record.get("pre_change_rate"))
    if not math.isfinite(change_value):
        return {"status": "error", "reason": "pre_change_val_unavailable"}
    if not math.isfinite(change_percent):
        return {"status": "error", "reason": "pre_change_rate_unavailable"}

    provider_return = change_percent / 100.0
    previous_close = pre_price - change_value
    rate_denominator = 1.0 + provider_return
    if previous_close <= 0 or rate_denominator <= 0:
        return {"status": "error", "reason": "invalid_provider_reference"}
    reference_from_rate = pre_price / rate_denominator
    derived_return = pre_price / previous_close - 1.0
    rate_difference = abs(derived_return - provider_return)
    reference_difference = abs(previous_close / reference_from_rate - 1.0)
    passed = rate_difference <= tolerance and reference_difference <= tolerance

    snapshot_close = _finite(record.get("prev_close_price"))
    if math.isfinite(snapshot_close) and snapshot_close > 0:
        snapshot_difference = abs(snapshot_close / previous_close - 1.0)
        snapshot_status = "consistent" if snapshot_difference <= tolerance else "rejected_mismatch"
    else:
        snapshot_difference = None
        snapshot_status = "unavailable"
    return {
        "status": "pass" if passed else "error",
        "reason": "consistent" if passed else "provider_fields_conflict",
        "premarket_price": pre_price,
        "previous_regular_close": previous_close,
        "premarket_return": derived_return,
        "provider_premarket_return": provider_return,
        "provider_change_value": change_value,
        "rate_absolute_difference": rate_difference,
        "reference_absolute_difference": reference_difference,
        "snapshot_prev_close_price": snapshot_close if math.isfinite(snapshot_close) else None,
        "snapshot_prev_close_status": snapshot_status,
        "snapshot_prev_close_absolute_difference": snapshot_difference,
        "return_denominator_field": "pre_price_minus_pre_change_val",
        "forbidden_denominator_fields": ["last_price", "prev_close_price"],
        "tolerance": tolerance,
    }
