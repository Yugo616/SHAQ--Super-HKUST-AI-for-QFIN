"""Read-only historical backup. No broker, live quote, or options endpoints.

Alpaca bars contract: https://docs.alpaca.markets/us/reference/stockbars
This adapter deliberately refuses current-session requests: delayed SIP must
not silently replace the current premarket snapshot or official auction labels.
"""
from __future__ import annotations

import hashlib
import math
import re
from contextlib import nullcontext
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from .data_providers import DataProviderError
from .data_retry import failure_diagnostic


ET = ZoneInfo('America/New_York')


class AlpacaHistoryProvider:
    capabilities = {'daily_bars'}
    provider_id = 'alpaca-sip'
    endpoint = 'https://data.alpaca.markets/v2/stocks/bars'

    def __init__(self, *, key_id, secret_key, timeout_seconds=30, max_pages=100,
                 client=None, now=None):
        if not key_id or not secret_key:
            raise DataProviderError('备用行情尚未连接：需要 Alpaca API 授权。',
                                    diagnostic={'kind': 'auth_error', 'provider': self.provider_id})
        self._headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}
        self.timeout = timeout_seconds
        self.max_pages = max_pages
        self.client = client
        self.now = now or (lambda: datetime.now(ET))
        self.receipts = []
        self.source_documents = {}

    def _error(self, kind, message, **details):
        return DataProviderError(message, diagnostic={
            'kind': kind, 'provider': self.provider_id, 'stage': 'history', **details})

    def history(self, symbols, *, start, end, interval='1d', prepost=False):
        if interval not in {'1d', '1m'} or prepost:
            raise self._error('provider_error', '备用源仅支持历史日线和历史一分钟行情。')
        if not symbols or any(not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,31}', s) for s in symbols):
            raise self._error('protocol_error', '备用行情股票代码不合规。')
        captured = self.now().astimezone(ET)
        left = datetime.combine(start, time.min, ET)
        right = datetime.combine(end, time.min, ET)
        if left >= right or right > captured - timedelta(minutes=15):
            raise self._error('provider_error', '备用源不替代当前盘前数据：仅请求已结束的历史时段。')
        params = {'symbols': ','.join(sorted(set(symbols))),
                  'timeframe': '1Day' if interval == '1d' else '1Min',
                  'start': left.isoformat(), 'end': right.isoformat(),
                  'adjustment': 'raw', 'feed': 'sip', 'sort': 'asc', 'limit': 10000,
                  'asof': start.isoformat()}
        output = {s: {} for s in symbols}
        seen_tokens = set()
        receipts = []
        context = nullcontext(self.client) if self.client is not None else httpx.Client(timeout=self.timeout)
        with context as client:
            for _ in range(self.max_pages):
                try:
                    response = client.get(self.endpoint, params=params, headers=self._headers,
                                          timeout=self.timeout, follow_redirects=False)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    diagnostic = failure_diagnostic(exc, 'history')
                    raise self._error(diagnostic['kind'],
                        f"Alpaca 历史行情请求失败（HTTP {response.status_code}）。",
                        http_status=response.status_code) from None
                except httpx.RequestError as exc:
                    kind = 'timeout' if isinstance(exc, httpx.TimeoutException) else 'connection_error'
                    raise self._error(kind, 'Alpaca 历史行情连接失败。') from None
                captured = self.now().astimezone(ET)
                digest = hashlib.sha256(response.content).hexdigest()
                self.source_documents[digest] = response.content
                try:
                    body = response.json()
                    bars = body['bars']
                    if not isinstance(bars, dict) or set(bars) - set(output):
                        raise ValueError()
                    for symbol, rows in bars.items():
                        for bar in rows:
                            observed = datetime.fromisoformat(bar['t'].replace('Z', '+00:00'))
                            if observed.tzinfo is None:
                                raise ValueError()
                            observed = observed.astimezone(ET)
                            if observed == right:  # Alpaca's end is inclusive; our contract isn't.
                                continue
                            if not left <= observed < right:
                                raise ValueError()
                            numbers = {name: float(bar[key]) for name, key in (
                                ('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v'))}
                            if (not all(math.isfinite(v) for v in numbers.values())
                                    or numbers['volume'] < 0 or numbers['low'] <= 0
                                    or numbers['low'] > min(numbers['open'], numbers['close'])
                                    or numbers['high'] < max(numbers['open'], numbers['close'])):
                                raise ValueError()
                            stamp = (observed.date().isoformat() + 'T00:00:00'
                                     if interval == '1d' else observed.isoformat())
                            row = {'timestamp': stamp, **numbers, 'source_provider': self.provider_id,
                                   'source_feed': 'sip', 'price_adjustment': 'unadjusted',
                                   'timestamp_semantics': 'session_date' if interval == '1d' else 'minute_start',
                                   'captured_at': captured.isoformat()}
                            if stamp in output[symbol] and output[symbol][stamp] != row:
                                raise ValueError()
                            output[symbol][stamp] = row
                    token = body.get('next_page_token')
                    if token is not None and not isinstance(token, str):
                        raise ValueError()
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise self._error('protocol_error', 'Alpaca 返回不完整或不合规行情，未用于分析。') from None
                receipts.append({'provider': self.provider_id, 'source_uri': self.endpoint,
                    'captured_at': captured.isoformat(), 'interval': interval,
                    'start': left.isoformat(), 'end_exclusive': right.isoformat(),
                    'symbols': sorted(set(symbols)), 'feed': 'sip', 'adjustment': 'raw',
                    'response_sha256': digest})
                if not token:
                    self.receipts.extend(receipts)
                    return {s: [rows[k] for k in sorted(rows)] for s, rows in output.items()}
                if token in seen_tokens:
                    raise self._error('protocol_error', 'Alpaca 分页重复，未接受残缺行情。')
                seen_tokens.add(token)
                params['page_token'] = token
        raise self._error('protocol_error', 'Alpaca 历史行情超过分页上限，未接受残缺行情。')


class HistoricalFallbackProvider:
    """A collection-scoped circuit: only verified historical transport fallback.

    Must be explicitly constructed with an authorized/validated backup. No
    hidden credential discovery or automatic activation in existing profiles.
    """
    def __init__(self, primary, backup):
        self.primary, self.backup = primary, backup
        self.primary_failed = False
        self.fallback_events = []

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def history(self, symbols, *, start, end, interval='1d', prepost=False):
        if prepost or interval not in {'1d', '1m'}:
            return self.primary.history(symbols, start=start, end=end, interval=interval, prepost=prepost)
        primary_rows = {}
        reason = 'primary_circuit_open'
        if not self.primary_failed:
            try:
                primary_rows = self.primary.history(symbols, start=start, end=end, interval=interval)
                reason = 'missing_history'
            except DataProviderError as exc:
                reason = exc.diagnostic.get('kind')
                if reason not in {'timeout', 'connection_error', 'rate_limited', 'provider_unavailable'}:
                    raise
                self.primary_failed = True
                recover = getattr(self.primary, 'recover_history', None)
                if recover is not None and interval == '1d':
                    primary_rows = recover(symbols, start=start, end=end)
        missing = [s for s in symbols if not primary_rows.get(s)]
        if missing:
            replacement = self.backup.history(missing, start=start, end=end, interval=interval)
            primary_rows.update({s: replacement.get(s, []) for s in missing})
            self.fallback_events.append({'symbols': missing, 'reason': reason,
                'provider': self.backup.provider_id, 'interval': interval,
                'start': start.isoformat(), 'end_exclusive': end.isoformat()})
        return {s: primary_rows.get(s, []) for s in symbols}
