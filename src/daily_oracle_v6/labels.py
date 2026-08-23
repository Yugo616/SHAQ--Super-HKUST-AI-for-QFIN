from __future__ import annotations

from datetime import date, datetime
from typing import Any


class LabelError(ValueError):
    """A scientific label is not an exact unadjusted regular-session observation."""


LABEL_DOCUMENT_KEYS = {
    "schema_version",
    "run_id",
    "provider",
    "captured_at_et",
    "adjustment",
    "session_scope",
    "official_label_status",
    "trade_date",
    "session_close_et",
    "labels",
}


def validate_label_capture_time(
    *, now: datetime, session_close: datetime, trade_date: str, phase: str
) -> None:
    if phase not in {"provisional", "final"}:
        raise LabelError("label phase must be provisional or final")
    if now.tzinfo is None or session_close.tzinfo is None:
        raise LabelError("label times require explicit offsets")
    target = date.fromisoformat(trade_date)
    if session_close.date() != target:
        raise LabelError("session close does not match trade date")
    if now < session_close:
        raise LabelError("regular session has not closed")
    if phase == "final" and now.date() <= target:
        raise LabelError("final label requires a later-date independent reobservation")


def build_label_row(
    *,
    run_id: str,
    symbol: str,
    direction: str,
    trade_date: str,
    rows: list[dict[str, Any]],
    phase: str,
    adjustment: str,
) -> dict[str, Any]:
    if direction not in {"bullish", "bearish"}:
        raise LabelError("invalid forecast direction")
    if phase not in {"provisional", "final"}:
        raise LabelError("label phase must be provisional or final")
    if adjustment != "NONE":
        raise LabelError("scientific labels require unadjusted prices")
    matching = [row for row in rows if str(row.get("time_key", ""))[:10] == trade_date]
    if len(matching) != 1:
        raise LabelError("exactly one matching regular-session daily bar is required")
    row = matching[0]
    try:
        official_open = float(row["open"])
        official_close = float(row["close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LabelError("daily bar lacks valid open and close") from exc
    if official_open <= 0 or official_close <= 0:
        raise LabelError("daily bar prices must be positive")
    underlying_return = official_close / official_open - 1.0
    actual_direction = (
        "bullish" if official_close > official_open
        else ("bearish" if official_close < official_open else "neutral")
    )
    return {
        "forecast_id": f"{run_id}:{symbol}",
        "symbol": symbol,
        "forecast_direction": direction,
        "trade_date": trade_date,
        "session_scope": "US_regular_session",
        "price_basis": "unadjusted_OHLC",
        "official_open": official_open,
        "official_close": official_close,
        "official_open_to_close_return": underlying_return,
        "actual_direction": actual_direction,
        "correct": actual_direction == direction,
        "flat_outcome_policy": "neutral_and_wrong_for_bullish_or_bearish",
        "official_label_status": phase,
    }


def validate_label_document(
    document: dict[str, Any], frozen: dict[str, Any], *, expected_phase: str
) -> dict[str, Any]:
    if set(document) != LABEL_DOCUMENT_KEYS:
        raise LabelError("label document keys differ from the frozen contract")
    if (
        document.get("schema_version") != 6
        or document.get("run_id") != frozen.get("run_id")
        or document.get("provider") != "Futu OpenD"
        or document.get("adjustment") != "NONE"
        or document.get("session_scope") != "US_regular_session"
        or document.get("official_label_status") != expected_phase
    ):
        raise LabelError("label document metadata differs from the frozen contract")
    try:
        captured = datetime.fromisoformat(
            str(document["captured_at_et"]).replace("Z", "+00:00")
        )
        session_close = datetime.fromisoformat(
            str(document["session_close_et"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise LabelError("label document timestamps are invalid") from exc
    trade_date = str(document.get("trade_date", ""))
    validate_label_capture_time(
        now=captured,
        session_close=session_close,
        trade_date=trade_date,
        phase=expected_phase,
    )
    rows = document.get("labels")
    if not isinstance(rows, list):
        raise LabelError("labels must be a list")
    by_id = {str(row.get("forecast_id", "")): row for row in rows}
    if "" in by_id or len(by_id) != len(rows):
        raise LabelError("label forecast IDs are blank or duplicated")
    expected_ids = {row["forecast_id"] for row in frozen.get("predictions", [])}
    if set(by_id) != expected_ids:
        raise LabelError("labels differ from the frozen forecast set")
    canonical_rows = []
    for forecast in frozen.get("predictions", []):
        observed = by_id[forecast["forecast_id"]]
        rebuilt = build_label_row(
            run_id=frozen["run_id"],
            symbol=forecast["symbol"],
            direction=forecast["direction"],
            trade_date=trade_date,
            rows=[{
                "time_key": trade_date,
                "open": observed.get("official_open"),
                "close": observed.get("official_close"),
            }],
            phase=expected_phase,
            adjustment="NONE",
        )
        if observed != rebuilt:
            raise LabelError("label row differs from deterministic recomputation")
        canonical_rows.append(rebuilt)
    return {**document, "labels": canonical_rows}
