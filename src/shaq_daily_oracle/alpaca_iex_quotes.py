"""Bounded historical IEX quote retrieval for diagnostic OFI evidence.

Alpaca REST quote pages are retained verbatim. This client makes no claim that
the free tier supplies a complete session or that post-cutoff backfill was
available at the original cutoff.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .iex_orderflow import _epoch_ns, _valid_bbo, compute_iex_orderflow, compute_sip_quote_pressure
from .alpaca_bars import PREMARKET_START


_QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes"
_STOCK_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]*")


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_ns_text(value: int) -> str:
    seconds, fraction = divmod(value, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{fraction:09d}Z" if fraction else f"{base}Z"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _default_fetch(request: Request, timeout: float, max_bytes: int) -> bytes:
    # Never forward authentication headers to a URL supplied by a redirect.
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return response.read(max_bytes + 1)


def _fetch_bounded(
    request: Request, *, timeout: float, max_bytes: int, max_attempts: int,
    fetch: Callable[[Request, float, int], bytes],
    sleep: Callable[[float], None] = time.sleep,
    retry_backoff_seconds: float = 1.0, retry_wait_cap_seconds: float = 30.0,
) -> bytes:
    for attempt in range(max_attempts):
        delay = retry_backoff_seconds * (2 ** attempt)
        try:
            body = fetch(request, timeout, max_bytes)
            if not isinstance(body, bytes) or len(body) > max_bytes:
                raise ValueError("Alpaca quote response exceeds configured byte limit")
            return body
        except HTTPError as exc:
            if attempt + 1 == max_attempts or (exc.code != 429 and not 500 <= exc.code <= 599):
                raise
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            # Do not ignore a provider-requested wait longer than our budget.
            if not math.isfinite(delay) or delay > retry_wait_cap_seconds:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt + 1 == max_attempts:
                raise
        sleep(min(delay, retry_wait_cap_seconds))
    raise RuntimeError("unreachable quote retry state")


def collect_iex_quotes(
    *, key_id: str, secret: str, cutoff: str, observed_at: str,
    symbols: Iterable[str], window_seconds: int,
    max_pages_per_symbol: int, timeout_seconds: float,
    max_response_bytes: int, max_attempts: int,
    fetch: Callable[[Request, float, int], bytes] | None = None,
    clock: Callable[[], datetime] | None = None,
    feed: str = 'iex', delay_seconds: int = 0, safety_margin_seconds: int = 0,
    full_premarket_session: bool = False,
    retry_backoff_seconds: float = 1.0, retry_wait_cap_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Request short-window `feed=iex` quotes; never contacts Alpaca without keys.

    `observed_at` is the caller's observation time. Formal eligibility also
    requires this function's actual completion clock to remain before cutoff.
    """
    if not key_id.strip() or not secret.strip():
        raise ValueError("Alpaca market-data key ID and secret are required")
    if type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("window_seconds must be a positive integer")
    if type(max_pages_per_symbol) is not int or max_pages_per_symbol <= 0:
        raise ValueError("max_pages_per_symbol must be a positive integer")
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if any(not math.isfinite(v) or v < 0 for v in (retry_backoff_seconds, retry_wait_cap_seconds)):
        raise ValueError('quote retry waits must be finite and nonnegative')
    if feed not in {'iex', 'sip'}:
        raise ValueError('unsupported quote feed')
    if (type(delay_seconds) is not int or type(safety_margin_seconds) is not int
            or delay_seconds < 0 or safety_margin_seconds < 0
            or (feed == 'sip' and delay_seconds < 900)):
        raise ValueError('SIP historical quotes require at least fifteen minutes delay')
    calculate = compute_iex_orderflow if feed == 'iex' else compute_sip_quote_pressure
    cutoff_ns = _epoch_ns(cutoff)
    observed_ns = _epoch_ns(observed_at)
    end_ns = min(cutoff_ns, observed_ns) - (delay_seconds + safety_margin_seconds) * 1_000_000_000
    start_ns = end_ns - window_seconds * 1_000_000_000
    session_day = datetime.fromtimestamp(min(cutoff_ns, observed_ns) / 1_000_000_000,
                                        ZoneInfo('America/New_York')).date()
    session_start = datetime.combine(session_day, PREMARKET_START, ZoneInfo('America/New_York'))
    session_start_ns = _epoch_ns(session_start.isoformat())
    start_ns = session_start_ns if full_premarket_session else max(start_ns, session_start_ns)
    start_text = _utc_ns_text(start_ns)
    end_text = _utc_ns_text(end_ns)
    normalized_symbols = list(dict.fromkeys(str(value).strip().upper() for value in symbols))
    if not normalized_symbols or any(_STOCK_SYMBOL.fullmatch(value) is None for value in normalized_symbols):
        raise ValueError("at least one valid stock symbol is required")
    fetch = fetch or _default_fetch
    clock = clock or (lambda: datetime.now(timezone.utc))
    output: dict[str, Any] = {}
    for symbol in normalized_symbols:
        raw_pages: list[dict[str, str]] = []
        source_hash = hashlib.sha256()
        rows: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        failure: str | None = None
        http_status = None
        if end_ns < start_ns:
            calculation = calculate([], symbol=symbol, cutoff=cutoff)
            calculation["reason"] = "observation_before_requested_window"
        else:
            try:
                for _ in range(max_pages_per_symbol):
                    query = {
                        "symbols": symbol, "feed": feed, "start": start_text,
                        "end": end_text, "sort": "asc", "limit": 10000,
                        "asof": session_day.isoformat(),
                    }
                    if page_token is not None:
                        query["page_token"] = page_token
                    url = _QUOTES_URL + "?" + urlencode(query)
                    request = Request(url, headers={
                        "APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret,
                    })
                    body = _fetch_bounded(
                        request, timeout=timeout_seconds, max_bytes=max_response_bytes,
                        max_attempts=max_attempts, fetch=fetch,
                        sleep=sleep, retry_backoff_seconds=retry_backoff_seconds,
                        retry_wait_cap_seconds=retry_wait_cap_seconds,
                    )
                    raw_pages.append({
                        "request_uri": url, "body_utf8": body.decode("utf-8"),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    })
                    source_hash.update(len(body).to_bytes(8, "big"))
                    source_hash.update(body)
                    page = json.loads(body)
                    if not isinstance(page, dict) or not isinstance(page.get("quotes"), dict):
                        raise ValueError("Alpaca historical quote response is malformed")
                    if any(key != symbol for key in page["quotes"]):
                        raise ValueError("Alpaca quote response contains an unexpected symbol")
                    quote_rows = page["quotes"].get(symbol, [])
                    if not isinstance(quote_rows, list):
                        raise ValueError("Alpaca historical quote rows are malformed")
                    for raw in quote_rows:
                        if not isinstance(raw, dict):
                            raise ValueError("Alpaca historical quote row is malformed")
                        if raw.get("S", symbol) != symbol or raw.get("T", "q") != "q":
                            raise ValueError("Alpaca historical quote row has conflicting identity")
                        event_ns = _epoch_ns(raw.get("t", ""))
                        if start_ns <= event_ns <= end_ns:
                            rows.append({**raw, "T": "q", "S": symbol})
                    next_token = page.get("next_page_token")
                    if next_token is None:
                        break
                    if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                        raise ValueError("Alpaca quote pagination token is invalid or repeated")
                    seen_tokens.add(next_token)
                    page_token = next_token
                else:
                    raise ValueError("Alpaca quote pagination exceeded configured page limit")
                calculation = calculate(rows, symbol=symbol, cutoff=end_text)
                if feed == 'sip':
                    calculation = _sip_liquidity_view(rows, calculation, symbol=symbol,
                        cutoff=end_text, recent_window_seconds=window_seconds)
            except Exception as exc:
                failure = type(exc).__name__
                http_status = getattr(exc, 'code', None)
                calculation = calculate([], symbol=symbol, cutoff=end_text)
                calculation["reason"] = "alpaca_quote_collection_failed"
        output[symbol] = {
            "status": calculation["status"] if failure is None else "provider_error",
            "error_type": failure,
            "http_status": http_status,
            "calculation": calculation,
            "quote_count": len(rows),
            "page_count": len(raw_pages),
            "raw_pages": raw_pages,
            "raw_sha256": source_hash.hexdigest() if raw_pages else None,
            "normalization": "historical_rest_quote_plus_T_q_and_S_symbol",
        }
    completed = clock()
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise ValueError("collection clock must be timezone aware")
    formal = observed_ns <= cutoff_ns and _epoch_ns(_utc_text(completed)) <= cutoff_ns
    for value in output.values():
        value["formal_cutoff_eligible"] = formal and value["status"] in {'computed', 'liquidity_context'}
        latest = value['calculation'].get('last_quote_time')
        value['calculation']['last_quote_age_at_capture_seconds'] = (
            (_epoch_ns(_utc_text(completed)) - _epoch_ns(latest)) / 1_000_000_000 if latest else None)
    return {
        "provider": "Alpaca Market Data", "feed": feed, "source_uri": _QUOTES_URL,
        "delay_seconds": delay_seconds, "safety_margin_seconds": safety_margin_seconds,
        "scope": "IEX single venue" if feed == 'iex' else 'Delayed SIP consolidated best bid/ask', "requested_cutoff": cutoff,
        "requested_start": start_text, "requested_end": end_text,
        "observed_at": observed_at, "capture_completed_at": _utc_text(completed),
        "window_seconds": max(0, (end_ns - start_ns) / 1_000_000_000),
        "recent_window_seconds": window_seconds,
        "full_premarket_session": full_premarket_session,
        "formal_cutoff_eligible": formal,  # Timing only; see per-symbol eligibility.
        "post_cutoff_replay": not formal,
        "symbols": output,
    }


def _sip_liquidity_view(rows, session, *, symbol, cutoff, recent_window_seconds):
    """Do not turn an old or lone BBO into current order-flow evidence."""
    end = _epoch_ns(cutoff)
    seen, ambiguous = {}, set()
    for row in rows:
        stamp = _epoch_ns(row['t'])
        if stamp in seen and seen[stamp] != row:
            ambiguous.add(stamp)
        seen[stamp] = row
    valid = [row for row in rows if _epoch_ns(row['t']) <= end and _valid_bbo(row)
             and _epoch_ns(row['t']) not in ambiguous
             and isinstance(row.get('bx'), str) and row['bx']
             and isinstance(row.get('ax'), str) and row['ax']]
    recent = compute_sip_quote_pressure(
        [row for row in rows if _epoch_ns(row['t']) >= end - recent_window_seconds * 1_000_000_000],
        symbol=symbol, cutoff=cutoff)
    result = {**recent, 'recent_pressure_available': recent['status'] == 'computed',
              'session_context': session, 'recent_window_seconds': recent_window_seconds,
              'last_quote_age_seconds': None, 'latest_quote': None}
    if valid:
        last = valid[-1]
        result.update(last_quote_time=last['t'],
            last_quote_age_seconds=(end - _epoch_ns(last['t'])) / 1_000_000_000,
            latest_quote={'timestamp': last['t'], 'bid_price': last['bp'], 'ask_price': last['ap'],
                          'bid_size': last['bs'], 'ask_size': last['as'],
                          'bid_exchange': last['bx'], 'ask_exchange': last['ax'],
                          'unit': session['unit']})
        if recent['status'] != 'computed':
            result.update(status='liquidity_context', reason='no_recent_comparable_quote_changes',
                          inference_scope='timestamped_liquidity_context_not_current_direction')
    return result


def collect_quote_pressure(*, delay_seconds: int, safety_margin_seconds: int,
                           session_fallback: bool = True, **kwargs) -> dict[str, Any]:
    """Keep usable IEX data; retrieve delayed SIP only for missing symbols.

    Each selected symbol carries its own feed, window and timing. Both attempted
    raw responses remain in the archive; only the selected source is evidence.
    """
    primary = collect_iex_quotes(**kwargs)
    def unpack(result, symbol):
        return {**{k: v for k, v in result.items() if k != 'symbols'},
                **result['symbols'][symbol]}
    selected = {s: unpack(primary, s) for s in primary['symbols']}
    missing = [s for s, row in selected.items() if row['status'] != 'computed'
               and row.get('http_status') != 401]
    fallback = None
    if missing:
        fallback = collect_iex_quotes(**{**kwargs, 'symbols': missing}, feed='sip',
            delay_seconds=delay_seconds, safety_margin_seconds=safety_margin_seconds)
    attempt_fields = ('feed', 'status', 'quote_count', 'error_type', 'http_status', 'requested_start', 'requested_end')
    for symbol, old in list(selected.items()):
        attempts = [{k: old.get(k) for k in attempt_fields}]
        if fallback and symbol in fallback['symbols']:
            new = unpack(fallback, symbol)
            attempts.append({k: new.get(k) for k in attempt_fields})
            new['fallback_raw_pages'] = old['raw_pages']
            selected[symbol] = new
        selected[symbol]['attempts'] = attempts
    sparse = [s for s, row in selected.items() if row['feed'] == 'sip'
              and row['status'] in {'unavailable', 'liquidity_context'}]
    expanded = None
    if session_fallback and sparse:
        expanded = collect_iex_quotes(**{**kwargs, 'symbols': sparse}, feed='sip',
            delay_seconds=delay_seconds, safety_margin_seconds=safety_margin_seconds,
            full_premarket_session=True)
        for symbol in sparse:
            old, new = selected[symbol], unpack(expanded, symbol)
            attempts = old['attempts'] + [{k: new.get(k) for k in attempt_fields}]
            # A failed expansion cannot discard already usable quote context.
            if new['status'] == 'provider_error' and old['status'] == 'liquidity_context':
                old['fallback_raw_pages'] += new['raw_pages']
                old['attempts'] = attempts
            else:
                new['fallback_raw_pages'] = old.get('fallback_raw_pages', []) + old['raw_pages']
                new['attempts'] = attempts
                selected[symbol] = new
    return {**{k: v for k, v in primary.items() if k != 'symbols'}, 'feed': 'iex-with-delayed-sip-fallback',
            'scope': 'Per-symbol quoted liquidity; see each source and observed window',
            'capture_completed_at': (expanded or fallback or primary)['capture_completed_at'],
            'symbols': selected}
