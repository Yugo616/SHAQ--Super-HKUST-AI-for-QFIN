"""Pure IEX top-of-book order-flow imbalance from Alpaca quote messages.

This measures one venue's displayed BBO queue changes, not consolidated
market flow or buyer/seller-initiated trade volume. No API access occurs here.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import datetime
from itertools import groupby
from typing import Any, Iterable


IEX_EXCHANGE_CODE = "V"  # Alpaca's published exchange code for Investors Exchange.
_RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


def _epoch_ns(value: str) -> int:
    match = _RFC3339.fullmatch(value)
    if match is None:
        raise ValueError("quote and cutoff timestamps must be offset-aware RFC3339")
    day, clock, fraction, offset = match.groups()
    instant = datetime.fromisoformat(f"{day}T{clock}{'+00:00' if offset == 'Z' else offset}")
    seconds = calendar.timegm(instant.utctimetuple())
    return seconds * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def _event_ofi(previous: dict[str, Any], current: dict[str, Any]) -> int:
    """Cont et al. best-bid/best-ask event contribution, in round lots."""
    return (
        (current["bs"] if current["bp"] >= previous["bp"] else 0)
        - (previous["bs"] if current["bp"] <= previous["bp"] else 0)
        - (current["as"] if current["ap"] <= previous["ap"] else 0)
        + (previous["as"] if current["ap"] >= previous["ap"] else 0)
    )


def _valid_bbo(row: dict[str, Any]) -> bool:
    bid, ask = row.get("bp"), row.get("ap")
    bid_size, ask_size = row.get("bs"), row.get("as")
    return (
        type(bid) in (int, float) and type(ask) in (int, float)
        and math.isfinite(bid) and math.isfinite(ask)
        and bid > 0 and bid < ask
        and type(bid_size) is int and type(ask_size) is int
        and bid_size > 0 and ask_size > 0
    )


def compute_iex_orderflow(
    quotes: Iterable[dict[str, Any]], *, symbol: str, cutoff: str,
) -> dict[str, Any]:
    """Calculate displayed IEX BBO OFI for event-time-ordered quotes through cutoff."""
    cutoff_ns = _epoch_ns(cutoff)
    symbol = symbol.strip().upper()
    previous = None
    previous_ns = None
    ofi = events = valid_quotes = 0
    depth_sum = spread_bps_sum = 0.0
    first_mid = last_mid = None
    first_time = last_time = None
    invalid_book = False
    for row in quotes:
        event_ns = _epoch_ns(row["t"])
        if event_ns > cutoff_ns:
            continue
        if row.get("T") != "q" or row.get("S") != symbol:
            raise ValueError("IEX quote message or symbol mismatch")
        if row.get("bx") != IEX_EXCHANGE_CODE or row.get("ax") != IEX_EXCHANGE_CODE:
            raise ValueError("quote is not IEX-only on both sides")
        if previous_ns is not None and event_ns < previous_ns:
            raise ValueError("quote event times must be ordered")
        if previous_ns is not None and event_ns == previous_ns:
            if row == previous:
                continue
            raise ValueError("conflicting IEX quotes share an event time")
        if not _valid_bbo(row):
            invalid_book = True
            previous = None
            previous_ns = event_ns
            continue
        mid = (row["bp"] + row["ap"]) / 2
        depth_sum += (row["bs"] + row["as"]) / 2
        spread_bps_sum += (row["ap"] - row["bp"]) / mid * 10_000
        valid_quotes += 1
        if first_mid is None:
            first_mid = mid
        last_mid = mid
        if previous is not None:
            ofi += _event_ofi(previous, row)
            events += 1
        else:
            first_time = row["t"]
        previous = row
        previous_ns = event_ns
        last_time = row["t"]
    available = events > 0 and not invalid_book
    average_depth = depth_sum / valid_quotes if available else None
    return {
        "status": "computed" if available else "unavailable",
        "reason": None if available else (
            "invalid_iex_bbo" if invalid_book else "insufficient_iex_bbo_updates"
        ),
        "symbol": symbol,
        "scope": "IEX single venue",
        "full_market_capital_flow": False,
        "native_trade_aggressor": False,
        "data_origin_verified": False,
        "method": "Cont best-bid/best-ask queue OFI",
        "inference_scope": "observed_iex_quote_window_only",
        "full_session_coverage_verified": False,
        "unit": "round_lots",
        "ofi_round_lots": ofi if available else None,
        "average_visible_depth_round_lots_per_side": average_depth,
        "ofi_over_average_depth": ofi / average_depth if available else None,
        "first_mid_price": first_mid if available else None,
        "last_mid_price": last_mid if available else None,
        "mid_price_change": last_mid - first_mid if available else None,
        "average_spread_bps": spread_bps_sum / valid_quotes if available else None,
        "event_count": events if available else 0,
        "first_quote_time": first_time,
        "last_quote_time": last_time,
        "cutoff": cutoff,
    }


def compute_sip_quote_pressure(quotes, *, symbol: str, cutoff: str) -> dict[str, Any]:
    """Cont-style NBBO quote changes, not executions or a single exchange queue.

    Exchange changes, ambiguous timestamps and invalid books break the pair;
    they must not manufacture a queue-flow event. All exclusions are counted.
    """
    cutoff_ns = _epoch_ns(cutoff)
    previous = None
    last_ns = None
    events = ofi = excluded = switches = count = 0
    depth = spread = 0.0
    first = last = None
    for stamp, same_time in groupby(quotes, key=lambda row: _epoch_ns(row['t'])):
        if stamp > cutoff_ns:
            continue
        rows = list(same_time)
        if any(row.get('S') != symbol or row.get('T') != 'q' for row in rows):
            raise ValueError('SIP quote identity mismatch')
        if last_ns is not None and stamp < last_ns:
            raise ValueError('SIP quote times must be ordered')
        last_ns = stamp
        row = rows[0]
        # Resolve timestamp ambiguity before adding any pair or depth. Otherwise
        # the first row can create a spurious signal depending on input order.
        if any(other != row for other in rows[1:]):
            excluded += len(rows)
            previous = None
            continue
        if (not _valid_bbo(row) or not isinstance(row.get('bx'), str)
                or not isinstance(row.get('ax'), str) or not row['bx'] or not row['ax']):
            excluded += 1
            previous = None
            continue
        count += 1
        mid = (row['bp'] + row['ap']) / 2
        depth += (row['bs'] + row['as']) / 2
        spread += (row['ap'] - row['bp']) / mid * 10000
        if first is None:
            first = row
        last = row
        if previous is not None:
            if (row['bx'], row['ax']) == (previous['bx'], previous['ax']):
                ofi += _event_ofi(previous, row)
                events += 1
            else:
                switches += 1
        previous = row
    available = events > 0
    average_depth = depth / count if count else None
    return {
        'status': 'computed' if available else 'unavailable',
        'reason': None if available else 'insufficient_comparable_sip_quotes',
        'symbol': symbol, 'scope': 'Delayed SIP consolidated best bid/ask',
        'method': 'Cont-style quote imbalance within unchanged quoting venues',
        'reference': 'https://doi.org/10.1093/jjfinec/nbt003',
        'inference_scope': 'observed_delayed_quote_window_only',
        'native_trade_aggressor': False, 'full_market_capital_flow': False,
        'full_session_coverage_verified': False, 'data_origin_verified': False,
        'unit': 'shares' if cutoff[:10] >= '2025-11-03' else 'round_lots',
        'size_unit_source': 'https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change',
        'ofi_quote_size': ofi if available else None,
        'ofi_over_average_depth': ofi / average_depth if available else None,
        'average_visible_depth_quote_size_per_side': average_depth,
        'average_spread_bps': spread / count if count else None,
        'event_count': events, 'valid_quote_count': count,
        'excluded_quote_count': excluded, 'venue_switch_count': switches,
        'first_quote_time': first['t'] if first else None,
        'last_quote_time': last['t'] if last else None,
        'first_mid_price': (first['bp'] + first['ap']) / 2 if first else None,
        'last_mid_price': (last['bp'] + last['ap']) / 2 if last else None,
        'mid_price_change': ((last['bp'] + last['ap'] - first['bp'] - first['ap']) / 2
                             if first and last else None), 'cutoff': cutoff,
    }
