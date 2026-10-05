"""Isolated, read-only Alpaca Basic delayed-SIP premarket volume collector.

The caller must explicitly supply credentials and the documented delay policy.
This module is not wired into forecast collection or installed-app settings.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from contextlib import nullcontext
from datetime import date, datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo

import httpx


ET = ZoneInfo("America/New_York")
MINUTE = timedelta(minutes=1)
PREMARKET_START = day_time(4, 0)
PREMARKET_END = day_time(9, 30)
ALPACA_BASIC_MIN_DELAY_SECONDS = 15 * 60
MAX_API_PAGE_SIZE = 10_000
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,31}\Z")


class AlpacaBarsError(Exception):
    """A delayed volume sample is unavailable; no partial result is returned."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _et(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaBarsError("invalid_request", f"{name} requires a timezone")
    return value.astimezone(ET)


def probe_data_connection(*, key_id: str, secret_key: str, symbol: str,
                          now: datetime, timeout_seconds: float,
                          delay_seconds: int, client: httpx.Client | None = None) -> dict:
    """Read one historical sample per feed; never query a brokerage account.

    A sample proves authentication and that feed's response shape, not coverage
    of today's premarket session. Fixed official origin prevents key forwarding.
    """
    if not key_id.strip() or not secret_key.strip():
        raise ValueError('请填写 Alpaca 的 Key ID 和 Secret Key。')
    if not SYMBOL.fullmatch(symbol):
        raise ValueError('连接测试缺少有效股票代码。')
    now = _et(now, 'now')
    end = now - timedelta(seconds=max(delay_seconds, ALPACA_BASIC_MIN_DELAY_SECONDS))
    start = end - timedelta(days=7)  # Includes a prior session across weekends/holidays.
    results = {'checked_at': now.isoformat(), 'sample_symbol': symbol, 'feeds': {},
               'coverage_scope': 'historical_connection_test_not_live_readiness'}
    headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}
    own = client is None
    transport = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
    try:
        for endpoint, feed, capability in (('bars', 'sip', 'premarket_available'),
                                            ('quotes', 'iex', 'orderflow_available'),
                                            ('quotes', 'sip', 'orderflow_available')):
            params = {'symbols': symbol, 'feed': feed, 'start': start.isoformat(),
                      'end': end.isoformat(), 'limit': 1, 'sort': 'desc'}
            if endpoint == 'bars': params.update(timeframe='1Min', adjustment='raw')
            row_status = {'status': 'unavailable', 'message': ''}
            try:
                response = transport.get('https://data.alpaca.markets/v2/stocks/' + endpoint,
                                         headers=headers, params=params, timeout=timeout_seconds,
                                         follow_redirects=False)
                if response.status_code == 401:
                    raise ValueError('Alpaca 密钥未通过验证，请检查 Key ID 和 Secret Key。')
                if response.status_code != 200:
                    messages = {403: '此行情权限未开通', 429: '请求过于频繁，请稍后重试'}
                    row_status['message'] = messages.get(response.status_code, '行情服务暂时不可用，请稍后重试')
                else:
                    try:
                        rows = response.json()[endpoint].get(symbol, [])
                        if not rows:
                            row_status['message'] = '接口可访问，但未取得测试样本；请稍后重试'
                        else:
                            row = rows[0]
                            stamp = datetime.fromisoformat(row['t'].replace('Z', '+00:00'))
                            if stamp.tzinfo is None or not start <= stamp <= end:
                                raise ValueError('invalid sample timestamp')
                            if endpoint == 'bars':
                                _bar(row, session_date=stamp.astimezone(ET).date(), observed_at=now)
                            else:
                                bid, ask = float(row['bp']), float(row['ap'])
                                if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
                                        and float(row['bs']) > 0 and float(row['as']) > 0):
                                    raise ValueError('invalid quote')
                            current = stamp.astimezone(ET).date() == now.date()
                            row_status = {'status': 'available',
                                'message': '接口可用，已取得当日样本' if current else '接口可用；仅取得历史样本，不代表今天盘前已有覆盖',
                                'sample_at': stamp.isoformat(), 'current_session_sample': current}
                    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                        row_status['message'] = '返回的行情样本不完整或价格不合格，请稍后重试'
            except httpx.HTTPError:
                row_status['message'] = '无法连接行情服务，请检查网络后重试'
            results[capability] = results.get(capability, False) or row_status['status'] == 'available'
            results['feeds']['sip_quotes' if endpoint == 'quotes' and feed == 'sip' else feed] = row_status
    finally:
        if own: transport.close()
    if not results['premarket_available'] and not results['orderflow_available']:
        raise ValueError('连接未完成：' + '；'.join(f"{feed}：{row['message']}" for feed, row in results['feeds'].items()))
    return results


def _bar(row: dict, *, session_date: date, observed_at: datetime) -> tuple[datetime, int]:
    try:
        stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone missing")
        stamp = stamp.astimezone(ET)
        if stamp > observed_at or stamp.date() != session_date:
            raise ValueError("future or wrong-session bar")
        prices = [float(row[field]) for field in ("o", "h", "l", "c")]
        if (not all(math.isfinite(value) and value > 0 for value in prices)
                or prices[2] > min(prices[0], prices[3])
                or prices[1] < max(prices[0], prices[3])):
            raise ValueError("invalid OHLC")
        volume = row["v"]
        if type(volume) is not int or volume < 0:
            raise ValueError("invalid trade volume")
        return stamp, volume
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AlpacaBarsError("protocol_error", "Alpaca minute bar is invalid") from exc


def collect_premarket_volume(
    *, symbols: list[str], session_date: date, cutoff: datetime,
    observed_at: datetime, key_id: str, secret_key: str,
    base_url: str, feed: str, delay_seconds: int,
    safety_margin_seconds: int, delay_source: str,
    client: httpx.Client | None = None, max_pages: int = 100,
    max_retries: int = 1, retry_backoff_seconds: float = .25,
    timeout_seconds: float = 15, clock=None,
) -> dict:
    """Collect volume through the last complete, published premarket minute.

    At an 08:50 ET cutoff with 15-minute delay plus a 1-minute safety margin,
    the request ends at 08:34 and the last eligible minute starts at 08:33.
    No post-cutoff request or late page is accepted for a forecast sample.
    """
    if not isinstance(key_id, str) or not key_id or not isinstance(secret_key, str) or not secret_key:
        raise AlpacaBarsError("auth_error", "Alpaca market-data credentials are required")
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in symbols)):
        raise AlpacaBarsError("invalid_request", "Invalid symbol list")
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise AlpacaBarsError("invalid_request", "Invalid session date")
    cutoff_et, observed_et = _et(cutoff, "cutoff"), _et(observed_at, "observed_at")
    if (cutoff_et.date() != session_date or not PREMARKET_START < cutoff_et.time() <= PREMARKET_END
            or observed_et.date() != session_date or observed_et > cutoff_et):
        raise AlpacaBarsError("late_or_invalid_cutoff", "Premarket sample is not on time")
    if (not isinstance(base_url, str) or not base_url.startswith("https://")
            or not isinstance(feed, str) or feed != "sip"
            or not isinstance(delay_source, str) or not delay_source.strip()):
        raise AlpacaBarsError("invalid_request", "Source and delay provenance are required")
    if (type(delay_seconds) is not int or delay_seconds < ALPACA_BASIC_MIN_DELAY_SECONDS
            or type(safety_margin_seconds) is not int or safety_margin_seconds < 0
            or type(max_pages) is not int or max_pages < 1
            or type(max_retries) is not int or not 0 <= max_retries <= 3
            or not isinstance(retry_backoff_seconds, (int, float)) or retry_backoff_seconds < 0
            or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0):
        raise AlpacaBarsError("invalid_request", "Invalid collection limits or delay")

    start = datetime.combine(session_date, PREMARKET_START, ET)
    available_end = min(cutoff_et, observed_et - timedelta(
        seconds=delay_seconds + safety_margin_seconds))
    available_end = available_end.replace(second=0, microsecond=0)
    if available_end <= start:
        raise AlpacaBarsError("not_yet_available", "No completed delayed-SIP premarket minute")

    endpoint = base_url.rstrip("/") + "/v2/stocks/bars"
    requested = sorted(set(symbols))
    params = {"symbols": ",".join(requested), "timeframe": "1Min",
              "start": start.isoformat(), "end": available_end.isoformat(),
              "feed": feed, "adjustment": "raw", "sort": "asc",
              "asof": session_date.isoformat(), "limit": MAX_API_PAGE_SIZE}
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
    clock = clock or (lambda: datetime.now(ET))
    values: dict[str, dict[datetime, int]] = {symbol: {} for symbol in requested}
    provider_returned_symbols: set[str] = set()
    seen_tokens: set[str] = set()
    response_hashes: list[str] = []
    raw_pages: list[bytes] = []
    captured_at = observed_et
    context = nullcontext(client) if client is not None else httpx.Client(timeout=timeout_seconds)
    with context as session:
        for _ in range(max_pages):
            for attempt in range(max_retries + 1):
                try:
                    response = session.get(endpoint, params=params, headers=headers,
                                           timeout=timeout_seconds, follow_redirects=False)
                except httpx.TimeoutException as exc:
                    if attempt == max_retries:
                        raise AlpacaBarsError("timeout", "Alpaca minute bars timed out") from exc
                except httpx.RequestError as exc:
                    if attempt == max_retries:
                        raise AlpacaBarsError("connection_error", "Alpaca minute bars unavailable") from exc
                else:
                    if (response.status_code == 429 or 500 <= response.status_code <= 599) and attempt < max_retries:
                        pass
                    elif response.status_code == 403:
                        raise AlpacaBarsError("permission_denied", "Alpaca SIP access denied")
                    elif response.status_code == 401:
                        raise AlpacaBarsError("auth_error", "Alpaca authentication failed")
                    elif response.status_code == 429:
                        raise AlpacaBarsError("rate_limited", "Alpaca rate limit reached")
                    elif 500 <= response.status_code <= 599:
                        raise AlpacaBarsError("provider_unavailable", "Alpaca bars service unavailable")
                    elif response.status_code != 200:
                        raise AlpacaBarsError("provider_error", "Alpaca bars request failed")
                    else:
                        break
                if retry_backoff_seconds:
                    time.sleep(retry_backoff_seconds * (attempt + 1))

            captured_at = _et(clock(), "capture time")
            if captured_at > cutoff_et:
                raise AlpacaBarsError("late_capture", "Alpaca response arrived after cutoff")
            raw_pages.append(response.content)
            response_hashes.append(hashlib.sha256(response.content).hexdigest())
            try:
                body = response.json()
                bars = body["bars"]
                if not isinstance(bars, dict) or set(bars) - set(requested):
                    raise ValueError("invalid symbols")
                for symbol, rows in bars.items():
                    if not isinstance(rows, list):
                        raise ValueError("invalid rows")
                    if rows:
                        provider_returned_symbols.add(symbol)
                    for row in rows:
                        stamp, volume = _bar(row, session_date=session_date,
                                             observed_at=captured_at)
                        if not start <= stamp < datetime.combine(session_date, PREMARKET_END, ET):
                            raise ValueError("not premarket")
                        if stamp + MINUTE > available_end:
                            continue  # Inclusive API end can return an unpublished partial minute.
                        prior = values[symbol].get(stamp)
                        if prior is not None and prior != volume:
                            raise ValueError("conflicting duplicate")
                        values[symbol][stamp] = volume
                token = body.get("next_page_token")
                if token is not None and (not isinstance(token, str) or not token):
                    raise ValueError("invalid cursor")
            except AlpacaBarsError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise AlpacaBarsError("protocol_error", "Alpaca bars response is incomplete") from exc
            if token is None:
                break
            if token in seen_tokens:
                raise AlpacaBarsError("protocol_error", "Alpaca pagination loop")
            seen_tokens.add(token)
            params["page_token"] = token
        else:
            raise AlpacaBarsError("protocol_error", "Alpaca pagination incomplete")

    results = {}
    for symbol, bars in values.items():
        total = sum(bars.values())
        status = ("no_bars" if not bars else "observed_positive" if total > 0
                  else "all_zero_unconfirmed")
        results[symbol] = {"observed_volume": total if total > 0 else None,
                           "volume_status": status, "bar_count": len(bars),
                           "first_bar_start_et": min(bars).isoformat() if bars else None,
                           "last_bar_start_et": max(bars).isoformat() if bars else None}
    return {"symbols": results, "raw_pages": raw_pages, "metadata": {
        "provider": "alpaca", "feed": feed, "market_scope": "consolidated_us_sip",
        "source_uri": endpoint, "adjustment": "raw", "timeframe": "1Min",
        "bar_timestamp_semantics": "minute_start", "session_date": session_date.isoformat(),
        "cutoff_et": cutoff_et.isoformat(), "requested_at_et": observed_et.isoformat(),
        "captured_at_et": captured_at.isoformat(),
        "first_minute_start_et": start.isoformat(),
        "last_complete_minute_end_et": available_end.isoformat(),
        "delay_seconds": delay_seconds, "safety_margin_seconds": safety_margin_seconds,
        "delay_source": delay_source, "requested_symbol_count": len(requested),
        "provider_returned_symbol_count": len(provider_returned_symbols),
        "bar_symbol_count": sum(bool(v) for v in values.values()),
        "positive_volume_symbol_count": sum(r["volume_status"] == "observed_positive"
                                             for r in results.values()),
        "page_count": len(response_hashes), "response_sha256": response_hashes,
    }}
