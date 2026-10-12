"""Public-source adapters and incremental daily-bar storage."""
from __future__ import annotations
import csv
import io
import json
import math
import time
import httpx
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo
from .hashing import sha256_payload
from .settings import _atomic_json


class DailyBarCache:
    def __init__(self, provider, root: Path, *, overlap_days: int):
        self.provider, self.root, self.overlap = provider, root, overlap_days

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def _read(self, symbol):
        try:
            cached = json.loads((self.root / (sha256_payload(symbol) + '.json')).read_text(encoding='utf-8'))
            body = {key: cached[key] for key in ('start', 'end', 'rows')}
            start, end = date.fromisoformat(body['start']), date.fromisoformat(body['end'])
            rows = body['rows']
            if (cached.get('sha256') != sha256_payload(body) or start >= end
                    or not isinstance(rows, list) or not rows):
                return {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('timestamp'), str):
                    return {}
                stamp = datetime.fromisoformat(row['timestamp']).date()
                if not start <= stamp < end:
                    return {}
            return body
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Legacy or damaged caches lack a verified coverage interval. Fetch them
        # again; never infer a continuous history from the most recent row alone.
        return {}

    def _save(self, symbol, old, fresh, *, start, end):
        fresh = [r for r in fresh if start.isoformat() <= str(r['timestamp'])[:10] < end.isoformat()]
        if not fresh:
            return []
        connected = old and start.isoformat() <= old['end'] and old['start'] <= end.isoformat()
        merged = {str(r['timestamp']): r for r in old.get('rows', [])} if connected else {}
        merged.update({str(r['timestamp']): r for r in fresh})
        body = {'start': min(old['start'], start.isoformat()) if connected else start.isoformat(),
                'end': max(old['end'], end.isoformat()) if connected else end.isoformat(),
                'rows': [merged[k] for k in sorted(merged)]}
        _atomic_json(self.root / (sha256_payload(symbol) + '.json'),
                     {**body, 'sha256': sha256_payload(body)})
        return body['rows']

    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        if interval != "1d" or prepost:
            return self.provider.history(symbols, start=start, end=end, interval=interval, prepost=prepost)
        from .data_providers import DataProviderError
        output, groups, cached_by_symbol = {}, {}, {}
        last_failure = None
        for symbol in symbols:
            cached = self._read(symbol)
            cached_by_symbol[symbol] = cached
            # A later replay must not choose a fetch start after this request's end.
            rows = [r for r in cached.get('rows', []) if str(r['timestamp'])[:10] < end.isoformat()]
            requested_rows = [r for r in rows if r['timestamp'][:10] >= start.isoformat()]
            reuse = getattr(self.provider, 'reuse_prepared_history', None)
            if (reuse is not None and cached.get('start', '9999') <= start.isoformat()
                    and cached.get('end', '') >= end.isoformat()
                    and reuse(symbol, requested_rows, start=start, end=end)):
                output[symbol] = requested_rows
                continue
            recent = max((str(r.get("timestamp", ""))[:10] for r in rows), default="")
            beginning = start
            if recent and cached.get("start", "9999") <= start.isoformat():
                beginning = max(start, datetime.fromisoformat(recent).date() - timedelta(days=self.overlap))
            groups.setdefault(beginning, []).append(symbol)
            output[symbol] = rows
        for beginning, group in groups.items():
            try:
                fresh = self.provider.history(group, start=beginning, end=end, interval="1d")
            except DataProviderError as exc:
                partial = {}
                recover = getattr(self.provider, 'recover_history', None)
                if recover is not None:
                    partial = recover(group, start=beginning, end=end)
                    for symbol, rows in partial.items():
                        self._save(symbol, cached_by_symbol[symbol], rows, start=beginning, end=end)
                if not getattr(self.provider, 'supports_partial_history', False):
                    raise
                # A failed symbol must not invalidate independent successful symbols.
                # Preserve its error in provider diagnostics and never return stale rows.
                last_failure = exc
                fresh = partial
            for symbol in group:
                rows = fresh.get(symbol, [])
                if not rows:
                    # A failed provider update must not silently present old bars as fresh.
                    output[symbol] = []
                    continue
                full = self._save(symbol, cached_by_symbol[symbol], rows, start=beginning, end=end)
                output[symbol] = [r for r in full if start.isoformat() <= r["timestamp"][:10] < end.isoformat()]
        if last_failure is not None and not any(output.values()):
            raise last_failure
        retain = getattr(self.provider, 'retain_history_sources', None)
        if retain is not None:
            retain([row for rows in output.values() for row in rows])
        return output


class TableText(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == 'table': self.depth += 1
        if self.depth and tag in {'tr', 'td', 'th'}: self.parts.append(' | ')
    def handle_endtag(self, tag):
        if tag == 'table': self.depth = max(0, self.depth-1)
    def handle_data(self, data):
        if self.depth and data.strip(): self.parts.append(data.strip())


def earnings_expectations(rows):
    allowed = {'symbol', 'name', 'date', 'time', 'epsForecast', 'noOfEsts', 'fiscalQuarterEnding'}
    return [{key: value for key, value in row.items() if key in allowed} for row in rows]


def parse_fred_context(text, *, source, cutoff):
    """Daily OAS snapshot, knowable only at the enclosing packet's capture time.

    Observation dates are not release timestamps. The enclosing collector must
    reject captures after cutoff; this is not a historical-vintage backtester.
    """
    reader = csv.DictReader(io.StringIO(text))
    series = source['series_id']
    if not reader.fieldnames or not {'observation_date', series} <= set(reader.fieldnames):
        raise ValueError('FRED response has no requested credit series')
    observations = {}
    for row in reader:
        day = date.fromisoformat(row['observation_date'])
        if day >= cutoff.date() or row[series] in ('', '.', None):
            continue
        value = float(row[series])
        if not math.isfinite(value):
            raise ValueError('Non-finite credit spread')
        observations[day.isoformat()] = value
    values = [{'date': d, 'value': observations[d]} for d in sorted(observations)][-source['history_rows']:]
    if not values:
        raise ValueError('No completed daily credit observations')
    return {'series_id': series, 'units': source['units'], 'observations': values,
            'latest_change_basis_points': round((values[-1]['value']-values[-2]['value'])*100, 6) if len(values)>1 else None,
            'intraday': False, 'availability_semantics': 'known_at_actual_capture_not_observation_date',
            'interpretation_boundary': 'Daily option-adjusted credit spread, not a live spread. '
                'Observation date is not its publication time; no historical PIT claim.',
            'usage': 'local_internal_research_no_raw_redistribution'}


def _public_response(source, name, cutoff, config):
    """Bounded retries at the same public endpoint; never disable TLS checks."""
    from curl_cffi.requests import Session
    from curl_cffi.const import CurlHttpVersion
    from .data_retry import failure_diagnostic
    timeout = config['timeout_seconds']
    params = {'date': cutoff.date().isoformat()} if name == 'nasdaq_earnings' else None
    headers = {'User-Agent': 'SHAQ Daily Oracle Research', 'Accept': 'application/json,text/csv,text/html'}
    for attempt in range(3):
        try:
            if attempt < 2:
                with Session() as session:
                    response = session.get(source['url'], params=params, timeout=timeout,
                        allow_redirects=True, stream=False, headers=headers,
                        **({'http_version': CurlHttpVersion.V1_1} if attempt else {}))
                    response.raise_for_status()
                return response
            # A different TLS implementation can recover a failed curl handshake.
            # Bound the full streaming body as well as each socket operation.
            started = time.monotonic()
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream('GET', source['url'], params=params, headers=headers) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if time.monotonic() - started > timeout or size > 5_000_000:
                            raise TimeoutError('Public source response limit exceeded')
                        chunks.append(chunk)
                    return httpx.Response(response.status_code, content=b''.join(chunks),
                                          request=response.request)
        except Exception as exc:
            retryable = (failure_diagnostic(exc, None).get('retryable') is True
                         or isinstance(exc, httpx.RequestError))
            if not retryable or attempt == 2:
                raise
            time.sleep(config.get('retry_backoff_seconds', .25) * (attempt + 1))


def collect_public_context(config, *, cutoff, research_replay=False, clock=None):
    packets = []
    for name, source in config['sources'].items():
        if not source.get('enabled'):
            packets.append({'provider': name, 'status': 'not_enabled', 'source_uri': source['url']})
            continue
        try:
            response = _public_response(source, name, cutoff, config)
            captured = (clock or (lambda: datetime.now(ZoneInfo('America/New_York'))))()
            if source['kind'] == 'fred_csv':
                data = parse_fred_context(response.text, source=source, cutoff=cutoff)
            elif source['kind'] == 'csv':
                rows = list(csv.DictReader(io.StringIO(response.text)))
                rows = [r for r in rows if datetime.strptime(r['DATE'], '%m/%d/%Y').date() < cutoff.date()]
                data = rows[-30:]
            elif source['kind'] == 'html':
                parser = TableText(); parser.feed(response.text); data = ''.join(parser.parts)
                if not data: raise ValueError('没有找到利率数据表')
            else:
                data = earnings_expectations((response.json().get('data') or {}).get('rows') or [])
            eligible = captured <= cutoff
            usable = eligible or (research_replay and captured.date() == cutoff.date())
            packets.append({'provider': name, 'source_uri': source['url'], 'captured_at': captured.isoformat(),
                            'status': 'collected' if usable else 'after_cutoff', 'data': data,
                            'formal_cutoff_eligible': eligible, 'post_cutoff_replay': not eligible,
                            'raw': response.content})
        except Exception as exc:
            packets.append({'provider': name, 'source_uri': source['url'], 'status': 'provider_error', 'error': str(exc)})
    return packets
