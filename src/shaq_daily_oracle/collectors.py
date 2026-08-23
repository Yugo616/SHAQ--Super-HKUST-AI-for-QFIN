from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


class CollectorError(ValueError):
    """A deep-evidence collection cannot be interpreted safely."""


COLLECTION_STATUSES = {
    "collected",
    "not_entitled",
    "no_data",
    "provider_error",
    "not_applicable",
}


def collection_status(
    *, domain: str, symbol: str, status: str, captured_at_et: str,
    reason: str | None = None, record_count: int = 0,
) -> dict[str, Any]:
    if status not in COLLECTION_STATUSES:
        raise CollectorError("unsupported collection status")
    timestamp = datetime.fromisoformat(captured_at_et.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise CollectorError("collection status requires an offset timestamp")
    if record_count < 0 or (status == "collected") != (record_count > 0):
        raise CollectorError("collection status and record count disagree")
    return {
        "domain": domain,
        "symbol": symbol.strip().upper(),
        "status": status,
        "captured_at_et": captured_at_et,
        "reason": reason,
        "record_count": record_count,
    }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_capital_document(
    *, symbol: str, ticker_rows: Iterable[dict[str, Any]],
    order_book_samples: Iterable[dict[str, Any]], captured_at_et: str,
    window_start_et: str | None = None, window_end_et: str | None = None,
) -> dict[str, Any]:
    """Build event-level OFI evidence; aggregate vendor flow is deliberately rejected."""

    ticks = []
    buy_volume = sell_volume = neutral_volume = 0.0
    captured_time = datetime.fromisoformat(captured_at_et.replace("Z", "+00:00"))
    start = datetime.fromisoformat(window_start_et.replace("Z", "+00:00")) if window_start_et else None
    end = datetime.fromisoformat(window_end_et.replace("Z", "+00:00")) if window_end_et else None
    for row in ticker_rows:
        direction = str(row.get("ticker_direction", "")).upper()
        if direction not in {"BUY", "SELL", "NEUTRAL"}:
            continue
        volume = _finite(row.get("volume"))
        price = _finite(row.get("price"))
        timestamp = str(row.get("time", "")).strip()
        if volume is None or volume < 0 or price is None or price <= 0 or not timestamp:
            continue
        if start is not None and end is not None:
            try:
                observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=ZoneInfo("America/New_York"))
            except ValueError:
                try:
                    clock = datetime.strptime(timestamp, "%H:%M:%S").time()
                    observed = datetime.combine(captured_time.date(), clock, ZoneInfo("America/New_York"))
                except ValueError:
                    continue
            if observed < start or observed > end:
                continue
        ticks.append({"time": timestamp, "price": price, "volume": volume, "aggressor_side": direction})
        if direction == "BUY":
            buy_volume += volume
        elif direction == "SELL":
            sell_volume += volume
        else:
            neutral_volume += volume
    books = []
    for sample in order_book_samples:
        bids = sample.get("bid", [])
        asks = sample.get("ask", [])
        if not bids or not asks:
            continue
        best_bid = _finite(bids[0].get("price"))
        best_ask = _finite(asks[0].get("price"))
        bid_depth = sum(value for row in bids if (value := _finite(row.get("volume"))) is not None and value >= 0)
        ask_depth = sum(value for row in asks if (value := _finite(row.get("volume"))) is not None and value >= 0)
        depth_total = bid_depth + ask_depth
        if (
            best_bid is None or best_ask is None or best_bid <= 0 or best_ask < best_bid
            or depth_total <= 0
        ):
            continue
        midpoint = (best_bid + best_ask) / 2.0
        books.append({
            "observed_at_et": str(sample.get("observed_at_et", "")),
            "relative_spread": (best_ask - best_bid) / midpoint,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_imbalance": (bid_depth - ask_depth) / depth_total,
        })
    directional_volume = buy_volume + sell_volume
    if not ticks or not books or directional_volume <= 0:
        raise CollectorError("capital evidence requires event-level direction and order-book depth")
    ticks.sort(key=lambda row: (row["time"], row["price"], row["volume"], row["aggressor_side"]))
    books.sort(key=lambda row: row["observed_at_et"])
    median_depth = statistics.median(
        row["bid_depth"] + row["ask_depth"] for row in books
    )
    signed_volume = buy_volume - sell_volume
    return {
        "schema_version": 6,
        "domain": "capital",
        "symbol": symbol.strip().upper(),
        "captured_at_et": captured_at_et,
        "observation_window": {"start_et": window_start_et, "end_et": window_end_et},
        "method": "event_level_signed_flow_normalized_by_visible_depth",
        "trade_direction_semantics": "provider_classified_ticker_direction",
        "aggregate_vendor_money_flow_used": False,
        "metrics": {
            "buy_initiated_volume": buy_volume,
            "sell_initiated_volume": sell_volume,
            "neutral_volume": neutral_volume,
            "signed_volume_imbalance": signed_volume / directional_volume,
            "signed_volume_to_median_visible_depth": signed_volume / median_depth if median_depth > 0 else None,
            "median_relative_spread": statistics.median(row["relative_spread"] for row in books),
            "median_depth_imbalance": statistics.median(row["depth_imbalance"] for row in books),
        },
        "ticker_rows": ticks,
        "order_book_samples": books,
    }


def build_derivatives_document(
    *, symbol: str, underlying_price: float, option_rows: Iterable[dict[str, Any]],
    captured_at_et: str,
) -> dict[str, Any]:
    """Build distribution evidence without treating put/call volume or OI as direction."""

    spot = _finite(underlying_price)
    if spot is None or spot <= 0:
        raise CollectorError("a positive underlying price is required")
    cleaned = []
    for row in option_rows:
        strike = _finite(row.get("strike_price"))
        bid = _finite(row.get("bid_price"))
        ask = _finite(row.get("ask_price"))
        iv = _finite(row.get("option_implied_volatility"))
        expiry = str(row.get("strike_time", "")).strip()
        option_type = str(row.get("option_type", "")).upper()
        if (
            strike is None or strike <= 0 or bid is None or ask is None or bid < 0
            or ask < bid or iv is None or iv <= 0 or not expiry
            or option_type not in {"CALL", "PUT"}
        ):
            continue
        cleaned.append({
            "code": str(row.get("code", "")),
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0,
            "iv": iv,
            "volume": max(_finite(row.get("volume")) or 0.0, 0.0),
            "open_interest": max(_finite(row.get("option_open_interest")) or 0.0, 0.0),
        })
    if not cleaned:
        raise CollectorError("no complete option quotes were available")
    expiries = {}
    for expiry in sorted({row["expiry"] for row in cleaned}):
        rows = [row for row in cleaned if row["expiry"] == expiry]
        calls = [row for row in rows if row["option_type"] == "CALL"]
        puts = [row for row in rows if row["option_type"] == "PUT"]
        paired = []
        for strike in sorted({row["strike"] for row in calls} & {row["strike"] for row in puts}):
            call = min((row for row in calls if row["strike"] == strike), key=lambda row: abs(row["strike"] - spot))
            put = min((row for row in puts if row["strike"] == strike), key=lambda row: abs(row["strike"] - spot))
            paired.append((strike, call, put))
        if not paired:
            continue
        strike, call, put = min(paired, key=lambda value: abs(value[0] - spot))
        expiries[expiry] = {
            "atm_strike": strike,
            "implied_move_fraction": (call["mid"] + put["mid"]) / spot,
            "atm_put_minus_call_iv": put["iv"] - call["iv"],
            "call_volume": sum(row["volume"] for row in calls),
            "put_volume": sum(row["volume"] for row in puts),
            "call_open_interest": sum(row["open_interest"] for row in calls),
            "put_open_interest": sum(row["open_interest"] for row in puts),
        }
    if not expiries:
        raise CollectorError("no matched call-put maturity was available")
    return {
        "schema_version": 6,
        "domain": "derivatives",
        "symbol": symbol.strip().upper(),
        "captured_at_et": captured_at_et,
        "underlying_price": spot,
        "expiries": expiries,
        "directional_flow_eligible": False,
        "directional_flow_reason": "aggressor and opening-versus-closing semantics were not observed",
        "mechanical_put_call_or_oi_direction_forbidden": True,
    }


def _returns(bars: list[dict[str, Any]], window: int) -> list[float]:
    closes = [float(row["close"]) for row in bars[-(window + 1):]]
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]


def build_relationship_document(
    *, symbol: str, universe_rows: Iterable[dict[str, Any]],
    price_history: dict[str, Any], captured_at_et: str, exposure_window: int,
) -> dict[str, Any]:
    """Build PIT industry and residual exposure context; correlation is not an economic edge."""

    canonical = symbol.strip().upper()
    universe = {str(row.get("ticker", row.get("instrument", ""))).upper(): row for row in universe_rows}
    member = universe.get(canonical)
    if not member:
        raise CollectorError("candidate is absent from the PIT universe")
    stock_returns = _returns(price_history.get("stock_bars", []), exposure_window)
    sector_returns = _returns(price_history.get("sector_bars", []), exposure_window)
    if len(stock_returns) != exposure_window or len(sector_returns) != exposure_window:
        raise CollectorError("relationship exposure window is incomplete")
    sector_variance = statistics.variance(sector_returns)
    if sector_variance <= 0:
        raise CollectorError("sector exposure is unidentified")
    sector_beta = statistics.covariance(stock_returns, sector_returns) / sector_variance
    residuals = [stock - sector_beta * sector for stock, sector in zip(stock_returns, sector_returns, strict=True)]
    sector = str(member.get("gics_sector", "")).strip()
    subindustry = str(member.get("gics_sub_industry", "")).strip()
    peers = sorted(
        ticker for ticker, row in universe.items()
        if ticker != canonical and subindustry and str(row.get("gics_sub_industry", "")).strip() == subindustry
    )
    return {
        "schema_version": 6,
        "domain": "relationships",
        "symbol": canonical,
        "captured_at_et": captured_at_et,
        "pit_industry": {"gics_sector": sector, "gics_sub_industry": subindustry},
        "sector_benchmark": price_history.get("sector_benchmark"),
        "exposure_window_sessions": exposure_window,
        "sector_beta": sector_beta,
        "residual_volatility": statistics.stdev(residuals),
        "same_subindustry_symbols": peers,
        "named_economic_relationships": [],
        "correlation_is_not_an_economic_relationship": True,
    }
