"""Delayed SIP prints supplement bars; prints are not native aggressor flow.

Alpaca's condition-based bar filtering can omit odd-lot-only price bars.
https://docs.alpaca.markets/us/docs/market-data-faq
"""
from contextlib import nullcontext
from datetime import datetime, timedelta
import hashlib
import math
import time

import httpx
from .alpaca_bars import (AlpacaBarsError, ET, SYMBOL, _et, PREMARKET_START,
                          PREMARKET_END, ALPACA_BASIC_MIN_DELAY_SECONDS)


def collect_premarket_trades(*, symbols, session_date, cutoff, observed_at,
                            key_id, secret_key, delay_seconds, safety_margin_seconds,
                            timeout_seconds, client=None, clock=None, max_pages=100,
                            max_retries=1, retry_backoff_seconds=.25, research_replay=False):
    cutoff, observed = _et(cutoff, 'cutoff'), _et(observed_at, 'observed_at')
    if (not symbols or any(not SYMBOL.fullmatch(s) for s in symbols)
            or not key_id or not secret_key
            or cutoff.date() != session_date or observed.date() != session_date
            or (observed > cutoff and not research_replay) or not PREMARKET_START < cutoff.time() <= PREMARKET_END
            or delay_seconds < ALPACA_BASIC_MIN_DELAY_SECONDS or safety_margin_seconds < 0
            or max_pages < 1 or not 0 <= max_retries <= 3):
        raise AlpacaBarsError('invalid_request', 'Invalid delayed trade collection request')
    start = datetime.combine(session_date, PREMARKET_START, ET)
    end = (min(observed, cutoff) - timedelta(seconds=delay_seconds + safety_margin_seconds)).replace(second=0, microsecond=0)
    if end <= start:
        raise AlpacaBarsError('not_yet_available', 'No delayed premarket trade interval')
    endpoint = 'https://data.alpaca.markets/v2/stocks/trades'
    requested = sorted(set(symbols))
    params = dict(symbols=','.join(requested), start=start.isoformat(), end=end.isoformat(),
                  feed='sip', sort='asc', limit=10000, asof=session_date.isoformat())
    headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}
    clock = clock or (lambda: datetime.now(ET))
    rows = {s: {} for s in requested}
    pages, tokens = [], set()
    with nullcontext(client) if client is not None else httpx.Client(timeout=timeout_seconds) as session:
        for _ in range(max_pages):
            for attempt in range(max_retries + 1):
                try:
                    response = session.get(endpoint, params=params, headers=headers,
                                           timeout=timeout_seconds, follow_redirects=False)
                except httpx.RequestError as exc:
                    if attempt == max_retries:
                        raise AlpacaBarsError('connection_error', 'Trade data request failed') from exc
                else:
                    status = response.status_code
                    if status == 200:
                        break
                    transient = status == 429 or 500 <= status < 600
                    if not transient or attempt == max_retries:
                        kind = {401: 'auth_error', 403: 'permission_denied', 429: 'rate_limited'}.get(status, 'provider_error')
                        raise AlpacaBarsError(kind, 'Trade data service rejected request')
                time.sleep(retry_backoff_seconds * (attempt + 1))
            captured = _et(clock(), 'capture time')
            if captured.date() != session_date or (captured > cutoff and not research_replay):
                raise AlpacaBarsError('late_capture', 'Trade data arrived after cutoff')
            pages.append(response.content)
            try:
                body = response.json()
                returned = body['trades']
                if not isinstance(returned, dict) or set(returned) - set(rows):
                    raise ValueError('invalid symbols')
                for symbol, prints in returned.items():
                    for row in prints:
                        stamp = _et(datetime.fromisoformat(row['t'].replace('Z', '+00:00')), 'trade time')
                        price, size = float(row['p']), row['s']
                        if (not start <= stamp <= end or not math.isfinite(price) or price <= 0
                                or type(size) is not int or size <= 0
                                or not isinstance(row['x'], str) or not row['x']
                                or not isinstance(row['c'], list) or any(not isinstance(c, str) for c in row['c'])
                                or not isinstance(row['i'], (str, int))):
                            raise ValueError('invalid print')
                        identity = (row['x'], str(row['i']))
                        item = dict(timestamp=stamp.isoformat(), price=price, size=size,
                                    exchange=row['x'], trade_id=str(row['i']), conditions=row['c'], tape=row.get('z'))
                        if identity in rows[symbol] and rows[symbol][identity] != item:
                            raise ValueError('conflicting duplicate')
                        rows[symbol][identity] = item
                token = body.get('next_page_token')
                if token is not None and (not isinstance(token, str) or not token or token in tokens):
                    raise ValueError('invalid pagination')
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise AlpacaBarsError('protocol_error', 'Invalid trade data response') from exc
            if token is None:
                break
            tokens.add(token)
            params['page_token'] = token
        else:
            raise AlpacaBarsError('protocol_error', 'Trade pagination incomplete')
    output = {}
    for symbol, unique in rows.items():
        trades = sorted(unique.values(), key=lambda r: (r['timestamp'], r['exchange'], r['trade_id']))
        output[symbol] = dict(status='collected' if trades else 'no_data', trades=trades,
            trade_count=len(trades), reported_trade_volume=sum(t['size'] for t in trades) if trades else None,
            odd_lot_volume=sum(t['size'] for t in trades if 'I' in t['conditions']) if trades else None,
            first_trade_at=trades[0]['timestamp'] if trades else None,
            last_trade_at=trades[-1]['timestamp'] if trades else None, native_aggressor=None,
            interpretation_boundary='Raw reported prints including odd lots and sale conditions; not official bar volume, '
                'not inferred buy/sell direction. Do not add to overlapping bar volume or treat as fresh at cutoff.')
    return dict(symbols=output, raw_pages=pages, metadata=dict(provider='alpaca', feed='sip',
        source_uri=endpoint, requested_start=start.isoformat(), requested_end=end.isoformat(),
        captured_at_et=captured.isoformat(), delay_seconds=delay_seconds,
        formal_cutoff_eligible=observed <= cutoff and captured <= cutoff,
        post_cutoff_replay=observed > cutoff or captured > cutoff,
        safety_margin_seconds=safety_margin_seconds,
        response_sha256=[hashlib.sha256(p).hexdigest() for p in pages]))
