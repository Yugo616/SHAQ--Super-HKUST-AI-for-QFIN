"""Read OCC series-search open interest as prior-session structural context only."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .market_calendar import previous_market_session


class OccOptionsError(ValueError):
    """The OCC response cannot safely be attributed to the requested symbol."""


_ENDPOINT = "https://marketdata.theocc.com/series-search"
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}\Z")
_ET = ZoneInfo("America/New_York")


def _source_uri(symbol: str) -> str:
    if not _SYMBOL.fullmatch(symbol):
        raise OccOptionsError("invalid OCC underlying symbol")
    return f"{_ENDPOINT}?{urlencode({'symbolType': 'U', 'symbol': symbol})}"


def fetch_occ_series_search(
    symbol: str,
    *,
    timeout_seconds: int = 20,
    max_bytes: int = 5_000_000,
    streamer: Callable = httpx.stream,
) -> bytes:
    """Fetch only OCC's documented per-underlying batch report, with a size cap."""
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise OccOptionsError("OCC request limits must be positive")
    source_uri = _source_uri(symbol)
    for attempt in range(2):
        try:
            with streamer("GET", source_uri, timeout=timeout_seconds) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        continue
                    raise OccOptionsError(f"OCC series-search HTTP {response.status_code}")
                if response.status_code != 200:
                    raise OccOptionsError(f"OCC series-search HTTP {response.status_code}")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise OccOptionsError("OCC series-search response exceeds size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as exc:
            if attempt:
                raise OccOptionsError("OCC series-search timed out twice") from exc
    raise OccOptionsError("OCC series-search request failed")


def parse_occ_series_search(payload: bytes, *, symbol: str, captured_at: datetime) -> dict:
    """Retain standard-root call/put OI; never infer quote, IV, or trade direction."""
    source_uri = _source_uri(symbol)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise OccOptionsError("OCC capture time must be timezone aware")
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise OccOptionsError("OCC series-search is not UTF-8 text") from exc
    if not lines or lines[0].strip() != f"Series Search Results for {symbol}":
        raise OccOptionsError("OCC series-search symbol does not match request")
    header = "ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit"
    header_index = next((index for index, line in enumerate(lines) if line.startswith(header)), None)
    if header_index is None:
        raise OccOptionsError("OCC series-search column header is missing")

    contracts: list[dict] = []
    seen: dict[tuple[str, str, str], int] = {}
    excluded_adjusted_roots = 0
    source_rows = 0
    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) != 11 or fields[1] or not re.fullmatch(r"\d{4}", fields[2]):
            raise OccOptionsError("OCC series-search row layout changed")
        source_rows += 1
        root, year, month, day, integer, decimal, sides, call_oi, put_oi, _limit = (
            fields[0], *fields[2:]
        )
        if root != symbol:
            excluded_adjusted_roots += 1
            continue
        if not re.fullmatch(r"\d+", integer) or not re.fullmatch(r"\d{3}", decimal):
            raise OccOptionsError("invalid OCC option strike")
        try:
            expiration = datetime(int(year), int(month), int(day)).date().isoformat()
            strike = f"{int(integer)}.{decimal}"
            call_interest, put_interest = int(call_oi), int(put_oi)
        except ValueError as exc:
            raise OccOptionsError("invalid OCC standard-root series row") from exc
        if (int(integer) == 0 and int(decimal) == 0) or not re.fullmatch(r"C?\s*P?", sides) or not sides or min(call_interest, put_interest) < 0:
            raise OccOptionsError("invalid OCC option side or open interest")
        for side, interest in (("call", call_interest), ("put", put_interest)):
            if side[0].upper() in sides:
                key = expiration, strike, side
                if key in seen:
                    if seen[key] != interest:
                        raise OccOptionsError("conflicting OCC open interest for one series")
                    continue
                seen[key] = interest
                contracts.append({
                    "expiration": expiration,
                    "strike": strike,
                    "option_type": side,
                    "open_interest": interest,
                })
    if not source_rows or not contracts:
        raise OccOptionsError("OCC series-search has no standard-root series")

    summaries: dict[str, dict] = {}
    for contract in contracts:
        expiration = contract["expiration"]
        summary = summaries.setdefault(expiration, {
            "expiration": expiration,
            "call_contract_count": 0, "put_contract_count": 0,
            "call_open_interest": 0, "put_open_interest": 0,
        })
        side = contract["option_type"]
        summary[f"{side}_contract_count"] += 1
        summary[f"{side}_open_interest"] += contract["open_interest"]

    asof = previous_market_session(captured_at.astimezone(_ET).date()).session_date
    return {
        "symbol": symbol,
        "status": "collected",
        "provider": "OCC series-search",
        "source_uri": source_uri,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "captured_at": captured_at.isoformat(),
        "asof_session": asof.isoformat(),
        "asof_status": "inferred_previous_session_not_in_payload",
        "data_semantics": "prior_settlement_open_interest_not_quote_or_trade_flow",
        "excluded_adjusted_root_count": excluded_adjusted_roots,
        "contracts": contracts,
        "by_expiry_summary": [summaries[expiry] for expiry in sorted(summaries)],
    }
