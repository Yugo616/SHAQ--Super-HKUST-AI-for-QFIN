from __future__ import annotations

from typing import Any


class LedgerError(ValueError):
    """Scientific and trading outcomes were mixed or incomplete."""


def execution_cost_components(
    *,
    direction: str,
    quantity: float,
    entry_fill: float,
    exit_fill: float,
    entry_bid: float,
    entry_ask: float,
    exit_bid: float,
    exit_ask: float,
    fees: float,
) -> dict[str, Any]:
    if direction not in {"bullish", "bearish"}:
        raise LedgerError("invalid execution direction")
    values = (quantity, entry_fill, exit_fill, entry_bid, entry_ask, exit_bid, exit_ask)
    if any(value <= 0 for value in values) or fees < 0:
        raise LedgerError("execution cost inputs must be positive and fees nonnegative")
    if entry_ask < entry_bid or exit_ask < exit_bid:
        raise LedgerError("crossed arrival quote")
    entry_mid = (entry_bid + entry_ask) / 2.0
    exit_mid = (exit_bid + exit_ask) / 2.0
    entry_cost = (
        entry_fill / entry_mid - 1.0
        if direction == "bullish"
        else entry_mid / entry_fill - 1.0
    )
    exit_cost = (
        exit_mid / exit_fill - 1.0
        if direction == "bullish"
        else exit_fill / exit_mid - 1.0
    )
    spread = (entry_ask - entry_bid) / (2.0 * entry_mid) + (
        exit_ask - exit_bid
    ) / (2.0 * exit_mid)
    implementation = entry_cost + exit_cost
    fee_return = fees / (entry_fill * quantity)
    return {
        "spread_return": spread,
        "slippage_return": implementation - spread,
        "fee_return": fee_return,
        "borrow_return": 0.0,
        "impact_return": 0.0,
        "implementation_shortfall_return": implementation,
        "borrow_separately_identified": False,
        "impact_separately_identified": False,
        "paper_cost_interpretation": "borrow_is_in_broker_fees_and_impact_is_in_residual_slippage",
    }


def evaluation_record(
    *,
    forecast_id: str,
    direction: str = "bullish",
    official_open: float | None,
    official_close: float | None,
    entry_fill: float | None,
    exit_fill: float | None,
    arrival_price: float | None,
    fees: float | None,
) -> dict[str, Any]:
    if direction not in {"bullish", "bearish"}:
        raise LedgerError("direction must be bullish or bearish")
    official_return = None
    correct = None
    if official_open is not None and official_close is not None:
        if official_open <= 0:
            raise LedgerError("official open must be positive")
        official_return = official_close / official_open - 1.0
        correct = official_return > 0 if direction == "bullish" else official_return < 0
    actual_return = None
    if entry_fill is not None and exit_fill is not None:
        if entry_fill <= 0 or exit_fill <= 0:
            raise LedgerError("fills must be positive")
        actual_return = (
            exit_fill / entry_fill - 1.0
            if direction == "bullish"
            else entry_fill / exit_fill - 1.0
        )
    shortfall = None
    if arrival_price is not None and entry_fill is not None:
        if arrival_price <= 0 or entry_fill <= 0:
            raise LedgerError("arrival and fill prices must be positive")
        shortfall = (
            entry_fill / arrival_price - 1.0
            if direction == "bullish"
            else arrival_price / entry_fill - 1.0
        )
    return {
        "forecast_id": forecast_id,
        "direction": direction,
        "official_prediction_return": official_return,
        "official_label_scope": "US_regular_session_unadjusted_open_to_close",
        "flat_outcome_policy": "neutral_and_wrong_for_bullish_or_bearish",
        "prediction_correct": correct,
        "actual_fill_return": actual_return,
        "implementation_shortfall_vs_arrival": shortfall,
        "fees": fees,
        "p_committee_hit": None,
        "p_net_profit": None,
    }
