from __future__ import annotations

import json
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


def _observed_at(value: Any, *, session_date: datetime) -> datetime | None:
    """Parse a provider timestamp without silently inventing an observation time."""

    timestamp = str(value or "").strip()
    if not timestamp:
        return None
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=ZoneInfo("America/New_York"))
        return observed
    except ValueError:
        try:
            clock = datetime.strptime(timestamp, "%H:%M:%S").time()
        except ValueError:
            return None
        return datetime.combine(session_date.date(), clock, ZoneInfo("America/New_York"))


def build_capital_document(
    *, symbol: str, ticker_rows: Iterable[dict[str, Any]],
    order_book_samples: Iterable[dict[str, Any]], captured_at_et: str,
    window_start_et: str | None = None, window_end_et: str | None = None,
    formal_not_before_et: str | None = None, segment_count: int = 3,
) -> dict[str, Any]:
    """Build event-level OFI evidence; aggregate vendor flow is deliberately rejected."""

    ticks = []
    buy_volume = sell_volume = neutral_volume = 0.0
    captured_time = datetime.fromisoformat(captured_at_et.replace("Z", "+00:00"))
    start = datetime.fromisoformat(window_start_et.replace("Z", "+00:00")) if window_start_et else None
    end = datetime.fromisoformat(window_end_et.replace("Z", "+00:00")) if window_end_et else None
    not_before = (
        datetime.fromisoformat(formal_not_before_et.replace("Z", "+00:00"))
        if formal_not_before_et else None
    )
    if captured_time.tzinfo is None or captured_time.utcoffset() is None:
        raise CollectorError("capital capture time requires a timezone offset")
    if segment_count < 1:
        raise CollectorError("capital segment count must be positive")
    for row in ticker_rows:
        direction = str(row.get("ticker_direction", "")).upper()
        if direction not in {"BUY", "SELL", "NEUTRAL"}:
            continue
        volume = _finite(row.get("volume"))
        price = _finite(row.get("price"))
        timestamp = str(row.get("time", "")).strip()
        if volume is None or volume < 0 or price is None or price <= 0 or not timestamp:
            continue
        observed = _observed_at(timestamp, session_date=captured_time)
        if observed is None:
            continue
        if start is not None and end is not None:
            if observed < start or observed > end:
                continue
        ticks.append({
            "time": timestamp, "observed_at_et": observed.isoformat(),
            "price": price, "volume": volume, "aggressor_side": direction,
        })
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
        observed = _observed_at(sample.get("observed_at_et"), session_date=captured_time)
        if observed is None or (start is not None and observed < start) or (end is not None and observed > end):
            continue
        books.append({
            "observed_at_et": observed.isoformat(),
            "relative_spread": (best_ask - best_bid) / midpoint,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_imbalance": (bid_depth - ask_depth) / depth_total,
        })
    directional_volume = buy_volume + sell_volume
    if not ticks or not books or directional_volume <= 0:
        raise CollectorError("capital evidence requires event-level direction and order-book depth")
    ticks.sort(key=lambda row: (
        row["observed_at_et"], row["price"], row["volume"], row["aggressor_side"],
    ))
    books.sort(key=lambda row: row["observed_at_et"])
    median_depth = statistics.median(
        row["bid_depth"] + row["ask_depth"] for row in books
    )
    signed_volume = buy_volume - sell_volume
    directional_ticks = [row for row in ticks if row["aggressor_side"] != "NEUTRAL"]
    first_observed = datetime.fromisoformat(directional_ticks[0]["observed_at_et"])
    last_observed = datetime.fromisoformat(directional_ticks[-1]["observed_at_et"])
    span_seconds = (last_observed - first_observed).total_seconds()
    segments: list[list[dict[str, Any]]] = [[] for _ in range(segment_count)]
    for row in directional_ticks:
        observed = datetime.fromisoformat(row["observed_at_et"])
        if span_seconds <= 0:
            index = 0
        else:
            index = min(
                int(((observed - first_observed).total_seconds() / span_seconds) * segment_count),
                segment_count - 1,
            )
        segments[index].append(row)
    segment_imbalances: list[float | None] = []
    segment_windows = []
    for index, segment in enumerate(segments):
        buy = sum(row["volume"] for row in segment if row["aggressor_side"] == "BUY")
        sell = sum(row["volume"] for row in segment if row["aggressor_side"] == "SELL")
        segment_imbalances.append((buy - sell) / (buy + sell) if buy + sell else None)
        lower = first_observed if span_seconds <= 0 else first_observed + (last_observed - first_observed) * (index / segment_count)
        upper = last_observed if span_seconds <= 0 else first_observed + (last_observed - first_observed) * ((index + 1) / segment_count)
        segment_windows.append({
            "start_et": lower.isoformat(), "end_et": upper.isoformat(),
            "directional_tick_count": len(segment),
        })
    persistence_observable = all(value is not None for value in segment_imbalances)
    ticker_start = datetime.fromisoformat(ticks[0]["observed_at_et"])
    ticker_end = datetime.fromisoformat(ticks[-1]["observed_at_et"])
    book_start = datetime.fromisoformat(books[0]["observed_at_et"])
    book_end = datetime.fromisoformat(books[-1]["observed_at_et"])
    conservative_end = min(ticker_end, book_end)
    freshness_seconds = (end - conservative_end).total_seconds() if end else None
    formal_direction_eligible = bool(
        not_before is None
        or (
            ticker_end >= not_before and book_end >= not_before
            and (end is None or captured_time <= end)
            and persistence_observable
        )
    )
    first_price, last_price = ticks[0]["price"], ticks[-1]["price"]
    signed_imbalance = signed_volume / directional_volume
    depth_imbalance = statistics.median(row["depth_imbalance"] for row in books)
    price_return = last_price / first_price - 1.0 if first_price > 0 else None
    persistence = (
        (sum(value > 0 for value in segment_imbalances if value is not None)
         - sum(value < 0 for value in segment_imbalances if value is not None))
        / len(segment_imbalances)
        if persistence_observable else None
    )
    return {
        "schema_version": 6,
        "domain": "capital",
        "symbol": symbol.strip().upper(),
        "captured_at_et": captured_at_et,
        "observation_window": {
            "requested_start_et": window_start_et,
            "requested_end_et": window_end_et,
            "ticker_first_observed_at_et": ticker_start.isoformat(),
            "ticker_last_observed_at_et": ticker_end.isoformat(),
            "order_book_first_observed_at_et": book_start.isoformat(),
            "order_book_last_observed_at_et": book_end.isoformat(),
            "actual_conservative_end_et": conservative_end.isoformat(),
            "capture_completed_at_et": captured_time.isoformat(),
            "freshness_seconds_at_cutoff": freshness_seconds,
            "formal_not_before_et": formal_not_before_et,
        },
        "method": "event_level_signed_flow_normalized_by_visible_depth",
        "trade_direction_semantics": "provider_classified_ticker_direction",
        "aggregate_vendor_money_flow_used": False,
        "state_components": {
            "active_trade_pressure": (
                "buy" if signed_imbalance > 0 else "sell" if signed_imbalance < 0 else "balanced"
            ),
            "visible_book_capacity": (
                "bid_heavier" if depth_imbalance > 0 else "ask_heavier" if depth_imbalance < 0 else "balanced"
            ),
            "pressure_persistence": (
                "persistent_buy" if persistence == 1
                else "persistent_sell" if persistence == -1
                else "mixed" if persistence is not None
                else "unobservable"
            ),
            "observed_price_response": (
                "rising" if price_return and price_return > 0
                else "falling" if price_return and price_return < 0
                else "flat"
            ),
            "supported_horizon": "premarket_short_horizon" if formal_direction_eligible else "diagnostic_only",
        },
        "metrics": {
            "buy_initiated_volume": buy_volume,
            "sell_initiated_volume": sell_volume,
            "neutral_volume": neutral_volume,
            "signed_volume_imbalance": signed_imbalance,
            "signed_volume_to_median_visible_depth": signed_volume / median_depth if median_depth > 0 else None,
            "median_relative_spread": statistics.median(row["relative_spread"] for row in books),
            "median_depth_imbalance": depth_imbalance,
            "segment_signed_imbalances": segment_imbalances,
            "segment_windows": segment_windows,
            "flow_direction_persistence_observable": persistence_observable,
            "flow_direction_persistence": persistence,
            "corresponding_price_return": price_return,
            "formal_direction_eligible": formal_direction_eligible,
            "supported_horizon": "premarket_short_horizon" if formal_direction_eligible else "diagnostic_only",
        },
        "ticker_rows": ticks,
        "order_book_samples": books,
    }


def build_capital_analysis(raw: bytes) -> bytes:
    """Project immutable event-level OFI into the exact fields used by the Agent."""

    try:
        source = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("capital source is not valid UTF-8 JSON") from exc
    required = {
        "symbol", "captured_at_et", "observation_window", "method",
        "trade_direction_semantics", "aggregate_vendor_money_flow_used",
        "state_components", "metrics", "ticker_rows", "order_book_samples",
    }
    if not required.issubset(source):
        raise CollectorError("capital analysis source is incomplete")
    if not isinstance(source["ticker_rows"], list) or not isinstance(source["order_book_samples"], list):
        raise CollectorError("capital raw observations are invalid")
    view = {
        "schema_version": 1,
        "transform": "capital_ofi_analysis_view_v1",
        "domain": "capital",
        "symbol": source["symbol"],
        "captured_at_et": source["captured_at_et"],
        "observation_window": source["observation_window"],
        "method": source["method"],
        "trade_direction_semantics": source["trade_direction_semantics"],
        "aggregate_vendor_money_flow_used": source["aggregate_vendor_money_flow_used"],
        "state_components": source["state_components"],
        "metrics": source["metrics"],
        "source_observation_counts": {
            "ticker_rows": len(source["ticker_rows"]),
            "order_book_samples": len(source["order_book_samples"]),
        },
        "representation_note": (
            "Metrics are copied from the reproducible OFI document; raw ticks and "
            "book samples remain hash-bound audit evidence."
        ),
    }
    return (json.dumps(view, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_derivatives_document(
    *, symbol: str, underlying_price: float, option_rows: Iterable[dict[str, Any]],
    captured_at_et: str, option_events: Iterable[dict[str, Any]] = (),
    oi_increase_confirmations: dict[str, bool] | None = None,
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
    confirmations = oi_increase_confirmations or {}
    directional_events = []
    for row in option_events:
        side = str(row.get("ticker_type", "")).upper()
        option_type = str(row.get("option_type", "")).upper()
        strategy = str(row.get("strategy_type", "")).upper()
        order_types = [str(value).upper() for value in row.get("order_type_list", [])]
        code = str(row.get("option_code", ""))
        multi_leg = strategy not in {"", "N/A", "NONE", "UNKNOWN", "SINGLE"} or any(
            token in value for value in order_types for token in ("MULTI", "CROSS")
        )
        if side not in {"BUY", "SELL"} or option_type not in {"CALL", "PUT"} or multi_leg or not code:
            continue
        stock_direction = (
            "bullish" if (side, option_type) in {("BUY", "CALL"), ("SELL", "PUT")}
            else "bearish"
        )
        directional_events.append({
            "option_code": code, "fill_time": row.get("fill_time"),
            "ticker_type": side, "option_type": option_type,
            "turnover": _finite(row.get("turnover")), "vo_ratio": _finite(row.get("vo_ratio")),
            "strategy_type": strategy or None, "order_type_list": order_types,
            "candidate_stock_direction": stock_direction,
            "oi_increase_confirmed": confirmations.get(code) is True,
        })
    confirmed = [row for row in directional_events if row["oi_increase_confirmed"]]
    confirmed_directions = {row["candidate_stock_direction"] for row in confirmed}
    directional_eligible = len(confirmed_directions) == 1 and bool(confirmed)
    return {
        "schema_version": 6,
        "domain": "derivatives",
        "symbol": symbol.strip().upper(),
        "captured_at_et": captured_at_et,
        "underlying_price": spot,
        "expiries": expiries,
        "option_event_rows": directional_events,
        "directional_flow_eligible": directional_eligible,
        "confirmed_direction": next(iter(confirmed_directions)) if directional_eligible else None,
        "directional_flow_reason": (
            "single-leg active-side event was followed by same-contract OI increase"
            if directional_eligible else
            "chain is usable for distribution; directional events remain unconfirmed until later OI increase"
        ),
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
    summary = price_history.get("benchmark_exposure_summary", {})
    exposures = {
        benchmark: float(values["126"])
        for benchmark, values in summary.items() if "126" in values
    }
    stability = {
        benchmark: {window: float(beta) for window, beta in values.items() if window in {"63", "252"}}
        for benchmark, values in summary.items()
    }
    benchmark_map = price_history.get("benchmark_bars_by_symbol", {})
    for benchmark, bars in sorted(benchmark_map.items()):
        returns_126 = _returns(bars, exposure_window)
        if len(returns_126) != exposure_window or statistics.variance(returns_126) <= 0:
            continue
        beta_126 = statistics.covariance(stock_returns, returns_126) / statistics.variance(returns_126)
        exposures[benchmark] = beta_126
        checks = {}
        for window in (63, 252):
            stock_check = _returns(price_history.get("stock_bars", []), window)
            benchmark_check = _returns(bars, window)
            if len(stock_check) == window and len(benchmark_check) == window and statistics.variance(benchmark_check) > 0:
                checks[str(window)] = statistics.covariance(stock_check, benchmark_check) / statistics.variance(benchmark_check)
        stability[benchmark] = checks
    return {
        "schema_version": 6,
        "domain": "relationships",
        "symbol": canonical,
        "captured_at_et": captured_at_et,
        "pit_industry": {"gics_sector": sector, "gics_sub_industry": subindustry},
        "sector_benchmark": price_history.get("sector_benchmark"),
        "exposure_window_sessions": exposure_window,
        "sector_beta": sector_beta,
        "multi_etf_beta_126": exposures,
        "beta_stability_checks_63_252": stability,
        "primary_exposure": max(exposures, key=lambda name: abs(exposures[name])) if exposures else price_history.get("sector_benchmark"),
        "residual_volatility": statistics.stdev(residuals),
        "same_subindustry_symbols": peers,
        "named_economic_relationships": [],
        "correlation_is_not_an_economic_relationship": True,
    }
