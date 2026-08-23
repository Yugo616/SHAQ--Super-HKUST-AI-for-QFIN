from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any


class PriceHistoryError(ValueError):
    """A historical price path is incomplete or semantically unsafe."""


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PriceHistoryError("capture time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceHistoryError("capture time requires an offset")
    return parsed


def _validate_bars(rows: list[dict[str, Any]], *, end_date: date, minimum_bars: int) -> list[dict[str, Any]]:
    if minimum_bars <= 0 or len(rows) < minimum_bars:
        raise PriceHistoryError("historical path lacks the configured minimum bars")
    output = []
    seen = set()
    for row in rows:
        timestamp = str(row.get("time_key", ""))
        try:
            bar_date = date.fromisoformat(timestamp[:10])
            values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
        except (KeyError, TypeError, ValueError) as exc:
            raise PriceHistoryError("historical bar fields are invalid") from exc
        if bar_date > end_date or timestamp in seen:
            raise PriceHistoryError("historical path is future-dated or duplicated")
        if any(not math.isfinite(value) for value in values.values()):
            raise PriceHistoryError("historical bar contains a non-finite value")
        if (
            min(values["open"], values["high"], values["low"], values["close"]) <= 0
            or values["volume"] < 0
            or values["high"] < max(values["open"], values["close"], values["low"])
            or values["low"] > min(values["open"], values["close"], values["high"])
        ):
            raise PriceHistoryError("historical OHLCV relationship is invalid")
        seen.add(timestamp)
        output.append({
            "time_key": timestamp,
            "open": values["open"],
            "high": values["high"],
            "low": values["low"],
            "close": values["close"],
            "volume": values["volume"],
        })
    output.sort(key=lambda row: row["time_key"])
    if output[-1]["time_key"][:10] != end_date.isoformat():
        raise PriceHistoryError("historical path does not end on the declared T-1 session")
    return output


def build_price_history_document(
    *, symbol: str, sector_benchmark: str, premarket_context: dict[str, Any],
    stock_bars: list[dict[str, Any]], sector_bars: list[dict[str, Any]],
    end_date: str, minimum_bars: int, captured_at_et: str, cutoff_et: str,
) -> dict[str, Any]:
    canonical = symbol.strip().upper()
    benchmark = sector_benchmark.strip().upper()
    if not canonical or not benchmark:
        raise PriceHistoryError("price-history identity is missing")
    end = date.fromisoformat(end_date)
    if _time(captured_at_et) > _time(cutoff_et):
        raise PriceHistoryError("price history was captured after cutoff")
    required_context = {
        "stock_premarket_return", "sector_premarket_return",
        "residual_premarket_return", "premarket_volume",
    }
    if not required_context.issubset(premarket_context):
        raise PriceHistoryError("premarket context is incomplete")
    context = {key: float(premarket_context[key]) for key in sorted(required_context)}
    if any(not math.isfinite(value) for value in context.values()):
        raise PriceHistoryError("premarket context contains a non-finite value")
    return {
        "schema_version": 6,
        "symbol": canonical,
        "sector_benchmark": benchmark,
        "session_scope": "US_regular_session",
        "adjustment": "NONE",
        "bar_end_date": end.isoformat(),
        "captured_at_et": captured_at_et,
        "cutoff_et": cutoff_et,
        "premarket_context": context,
        "stock_bars": _validate_bars(stock_bars, end_date=end, minimum_bars=minimum_bars),
        "sector_bars": _validate_bars(sector_bars, end_date=end, minimum_bars=minimum_bars),
        "corporate_action_status": "not_separately_observed",
    }
