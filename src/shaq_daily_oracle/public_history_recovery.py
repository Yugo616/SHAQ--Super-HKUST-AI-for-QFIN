"""Public chart transport and explicitly sourced historical-bar recovery.

Not used for account fills or official labels. Never substitutes a live quote
for a historical close, and never derives missing OHLC from other fields.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import math
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote
from functools import lru_cache

from .data_providers import DataProviderError, _yahoo_symbol

ET = ZoneInfo('America/New_York')


@lru_cache(maxsize=64)
def _sessions(start, end):
    from .market_calendar import _nyse
    return tuple(x.date().isoformat() for x in _nyse().schedule(
        start_date=start, end_date=end-timedelta(days=1)).index)


def _daily_complete(rows,start,end):
    if not rows or not all(valid_bar(r) for r in rows):
        return False
    dates={r['timestamp'][:10] for r in rows}
    expected=_sessions(start,end)
    return bool(expected and expected[-1] in dates and all(
        d in dates for d in expected if d >= min(dates)))


def _invalid(message):
    return DataProviderError(message, diagnostic={'kind': 'protocol_error', 'stage': 'history'})


def _number(value):
    if value is None:
        return None
    value = float(str(value).replace('$', '').replace(',', '').strip())
    if not math.isfinite(value):
        raise ValueError('non-finite price')
    return value


def valid_bar(row):
    try:
        o, h, l, c, v = (_number(row.get(k)) for k in ('open', 'high', 'low', 'close', 'volume'))
        return (None not in (o, h, l, c, v) and min(o, h, l, c) > 0 and v >= 0
                and l <= min(o, c) <= max(o, c) <= h)
    except (TypeError, ValueError):
        return False


def parse_chart(body, symbol, start, end, interval):
    try:
        chart = body['chart']
        if chart.get('error'):
            raise ValueError('chart error')
        result = chart['result'][0]
        if result['meta']['symbol'].upper() != _yahoo_symbol(symbol).upper():
            raise ValueError('symbol mismatch')
        zone = ZoneInfo(result['meta']['exchangeTimezoneName'])
        stamps = result.get('timestamp') or []
        quote = result['indicators']['quote'][0]
        if any(len(quote.get(k, [])) != len(stamps) for k in ('open', 'high', 'low', 'close', 'volume')):
            raise ValueError('array length mismatch')
        rows = []
        for i, stamp in enumerate(stamps):
            observed = datetime.fromtimestamp(stamp, zone)
            if not start <= observed.date() < end:
                continue
            rows.append({'timestamp': observed.date().isoformat() + 'T00:00:00'
                         if interval == '1d' else observed.isoformat(),
                         **{k: _number(quote[k][i]) for k in ('open', 'high', 'low', 'close', 'volume')},
                         'source_provider': 'yahoo-chart',
                         'timestamp_semantics': 'session_date' if interval == '1d' else 'minute_start'})
        if len({r['timestamp'] for r in rows}) != len(rows):
            raise ValueError('duplicate timestamp')
        return sorted(rows, key=lambda r: r['timestamp'])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise _invalid('Yahoo 历史响应字段不完整或股票身份不符。') from exc


def parse_nasdaq(body, symbol, start, end):
    try:
        data = body['data']
        if (body['status']['rCode'] != 200 or
                str(data['symbol']).upper().replace('/','.') != symbol.upper()):
            raise ValueError('status or symbol mismatch')
        records = data['tradesTable']['rows'] or []
        if int(data['totalRecords']) != len(records):
            raise ValueError('incomplete pagination')
        rows = []
        for record in records:
            day = datetime.strptime(record['date'], '%m/%d/%Y').date()
            if not start <= day < end:
                raise ValueError('out-of-window historical bar')
            row = {'timestamp': day.isoformat() + 'T00:00:00',
                   **{k: _number(record[k]) for k in ('open', 'high', 'low', 'close', 'volume')},
                   'source_provider': 'nasdaq-public-history', 'timestamp_semantics': 'session_date'}
            if not valid_bar(row):
                raise ValueError('invalid OHLCV')
            rows.append(row)
        if len({r['timestamp'] for r in rows}) != len(rows):
            raise ValueError('duplicate timestamp')
        return sorted(rows, key=lambda r: r['timestamp'])
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid('Nasdaq 历史响应不完整或股票身份不符。') from exc


def merge_daily(primary, backup):
    by_day = {r['timestamp'][:10]: r for r in primary}
    for row in backup:
        day = row['timestamp'][:10]
        if day not in by_day or not valid_bar(by_day[day]):
            by_day[day] = row
    return [by_day[d] for d in sorted(by_day)]


class PublicHistoryRecovery:
    """Bounded public requests; errors remain explicit per-symbol observations.

    Yahoo chart is the same source used by yfinance, without redundant timezone
    discovery/auth-cookie calls. Nasdaq supplements invalid historical daily bars
    only; intraday, fills and labels never fall back to its daily table.
    """
    def __init__(self, primary, config, *, checkpoint_root=None, etfs=()):
        self.primary, self.config = primary, config
        self.checkpoint_root = checkpoint_root
        self.etfs = set(etfs)
        self.diagnostics = []
        self.source_documents = {}
        self.supports_partial_history = True

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def _request(self, symbol, start, end, interval):
        from .hashing import sha256_payload
        config = {k:v for k,v in self.config.items() if k != 'parameter_bindings'}
        return {'source': sha256_payload(config), 'symbol':symbol,
                'start':start.isoformat(),'end':end.isoformat(),'interval':interval}

    def retain_history_sources(self, rows):
        for row in rows:
            digest = row.get('source_response_sha256')
            if not digest or digest in self.source_documents:
                continue
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise _invalid('历史来源校验值无效。')
            if not self.checkpoint_root:
                raise _invalid('历史来源回执缺失。')
            root=Path(self.checkpoint_root)
            paths=[root/'sources'/(digest+'.json')]
            paths.extend(root.parent.parent.glob('*/'+root.name+'/sources/'+digest+'.json'))
            saved=None
            for path in paths:
                try:
                    candidate=json.loads(path.read_text(encoding='utf-8'))
                    if (candidate['response_sha256']==digest and
                        hashlib.sha256(candidate['raw'].encode('utf-8')).hexdigest()==digest):
                        saved=candidate;break
                except (OSError,ValueError,KeyError,TypeError):
                    continue
            if saved is None:
                raise _invalid('历史来源回执缺失或损坏。')
            self.source_documents[digest]=saved

    def recover_history(self, symbols, *, start, end):
        from .collection_checkpoint import HistoryCheckpoint
        output={}
        for symbol in symbols:
            rows=HistoryCheckpoint(self.checkpoint_root,self._request(symbol,start,end,'1d')).read()
            if _daily_complete(rows,start,end):
                try:
                    self.retain_history_sources(rows)
                except DataProviderError:
                    continue
                output[symbol]=rows
        return output

    def _fetch(self, url, params):
        from curl_cffi.requests import Session
        from .data_retry import failure_diagnostic, failure_message
        from .settings import _atomic_json
        try:
            with Session(impersonate='chrome') as session:
                response = session.get(url, params=params,
                    timeout=self.profile.request_timeout_seconds, allow_redirects=False)
                response.raise_for_status()
                raw = response.content
                digest = hashlib.sha256(raw).hexdigest()
                document = {'source_uri': url, 'params': params,
                    'captured_at': datetime.now(ET).isoformat(), 'response_sha256': digest,
                    'raw': raw.decode('utf-8')}
                self.source_documents[digest] = document
                if self.checkpoint_root:
                    _atomic_json(Path(self.checkpoint_root) / 'sources' / (digest + '.json'), document)
                return json.loads(document['raw']), digest, document['captured_at']
        except DataProviderError:
            raise
        except Exception as exc:
            diagnostic = failure_diagnostic(exc, 'history')
            raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic) from exc

    def _chart(self, symbol, *, start, end, interval, prepost):
        left = int(datetime.combine(start, time.min, ET).timestamp())
        right = int(datetime.combine(end, time.min, ET).timestamp())
        uri = self.config['chart_url'].format(symbol=quote(_yahoo_symbol(symbol), safe=''))
        body, digest, captured = self.primary._request_with_retry(
            lambda: self._fetch(uri, {'period1': left, 'period2': right, 'interval': interval,
                                     'includePrePost': str(prepost).lower(), 'events': 'splits,div'}),
            stage='history', symbol=symbol)
        # Provider-declared absence is distinct from a transport failure.
        error = (body.get('chart') or {}).get('error') or {}
        if error.get('code') == 'Not Found':
            return []
        rows = parse_chart(body, symbol, start, end, interval)
        return [{**r, 'source_uri': uri, 'source_response_sha256': digest, 'captured_at': captured} for r in rows]

    def _nasdaq(self, symbol, *, start, end):
        uri = self.config['daily_backup_url'].format(symbol=quote(symbol, safe=''))
        body, digest, captured = self.primary._request_with_retry(
            lambda: self._fetch(uri, {'assetclass': 'etf' if symbol in self.etfs else 'stocks',
                'fromdate': start.isoformat(), 'todate': (end - timedelta(days=1)).isoformat(),
                'limit': self.config['daily_backup_max_rows']}), stage='history', symbol=symbol)
        rows = parse_nasdaq(body, symbol, start, end)
        return [{**r, 'source_uri': uri, 'source_response_sha256': digest, 'captured_at': captured} for r in rows]

    def _one(self, symbol, *, start, end, interval, prepost):
        from .collection_checkpoint import HistoryCheckpoint
        daily = interval == '1d' and not prepost
        request = self._request(symbol,start,end,interval)
        checkpoint = HistoryCheckpoint(self.checkpoint_root if daily else None, request)
        expected = _sessions(start,end) if daily else ()
        prior = expected[-1] if expected else None
        recovered = checkpoint.read()
        if _daily_complete(recovered,start,end):
            try:
                self.retain_history_sources(recovered)
                return recovered, 'reused'
            except DataProviderError:
                pass  # Refetch instead of accepting an unverifiable cache.
        try:
            rows = self._chart(symbol, start=start, end=end, interval=interval, prepost=prepost)
        except DataProviderError as exc:
            if not daily or exc.diagnostic.get('kind') not in {'timeout', 'connection_error', 'provider_unavailable'}:
                raise
            rows = []
        if daily:
            invalid_dates = [r['timestamp'][:10] for r in rows if not valid_bar(r)]
            observed={r['timestamp'][:10] for r in rows}
            if observed:
                invalid_dates += [d for d in expected if d >= min(observed) and d not in observed]
            prior_missing = not any(r['timestamp'][:10] == prior and valid_bar(r) for r in rows)
            if invalid_dates or prior_missing:
                # Nasdaq requires a nonzero date span even for one day's data.
                first = min(invalid_dates + ([prior] if prior_missing else [])) if rows else start.isoformat()
                begin = max(start, datetime.fromisoformat(first).date() - timedelta(days=1))
                rows = merge_daily(rows, self._nasdaq(symbol, start=begin, end=end))
            if not any(r['timestamp'][:10] == prior and valid_bar(r) for r in rows):
                raise DataProviderError('昨日收盘价仍缺失。', diagnostic={'kind': 'no_data', 'stage': 'history'})
            if not _daily_complete(rows,start,end):
                raise DataProviderError('历史交易日行情仍不完整。',diagnostic={'kind':'no_data','stage':'history'})
            checkpoint.save(rows)
        return rows, 'collected' if rows else 'no_data'

    def history(self, symbols, *, start, end, interval='1d', prepost=False):
        from .collection_worker import call_in_worker
        try:
            result = call_in_worker('public_history', self.profile, {
                'symbols': symbols, 'start': start.isoformat(), 'end': end.isoformat(),
                'interval': interval, 'prepost': prepost, 'recovery_config': self.config,
                'history_checkpoint_root': str(self.checkpoint_root) if self.checkpoint_root else None,
                'etfs': sorted(self.etfs),
            }, progress_observer=self.primary.progress_observer)
        except DataProviderError as exc:
            recovered=self.recover_history(symbols,start=start,end=end) if interval=='1d' and not prepost else {}
            self.diagnostics.extend({'symbol':s,'interval':interval,
                'status':'reused' if s in recovered else 'provider_error',
                'diagnostic':{} if s in recovered else exc.diagnostic} for s in symbols)
            raise
        self.diagnostics.extend(result['diagnostics'])
        self.source_documents.update(result['source_documents'])
        return result['rows']

    def _history_inline(self, symbols, *, start, end, interval='1d', prepost=False):
        from .data_retry import failure_diagnostic, sanitize_diagnostic
        from .research_progress import safe_observe
        workers = self.config.get('max_concurrency', 4)
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError('invalid market recovery concurrency')
        output = {s: [] for s in symbols}
        failures = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(self._one, s, start=start, end=end, interval=interval, prepost=prepost): s for s in symbols}
            for task in as_completed(pending):
                symbol = pending[task]
                try:
                    output[symbol], status = task.result()
                    detail = {'symbol': symbol, 'interval': interval, 'status': status}
                except Exception as exc:
                    diagnostic = sanitize_diagnostic(failure_diagnostic(exc, 'history'))
                    detail = {'symbol': symbol, 'interval': interval, 'status': 'provider_error',
                              'diagnostic': diagnostic}
                    failures.append(detail)
                self.diagnostics.append(detail)
                safe_observe(self.primary.progress_observer, stage='data_symbol_checked',
                             source='yahoo-chart+nasdaq-public-history', **detail)
        if failures and not any(output.values()):
            raise DataProviderError('本组行情所有来源均失败，成功缓存已保留。', diagnostic=failures[0]['diagnostic'])
        return output

    def recent_intraday(self, symbols, *, cutoff):
        rows = self.history(symbols, start=cutoff.date() - timedelta(days=5),
                            end=cutoff.date() + timedelta(days=1),
                            interval=self.profile.intraday_interval, prepost=True)
        interval=self.profile.intraday_interval
        if not interval.endswith('m') or not interval[:-1].isdigit():
            raise _invalid('不支持的盘前分钟周期。')
        duration=timedelta(minutes=int(interval[:-1]))
        return {s: [r for r in values if datetime.fromisoformat(r['timestamp']) + duration <= cutoff]
                for s, values in rows.items()}


def wrap_public_recovery(primary, package_root, *, checkpoint_root=None, etfs=()):
    path = Path(package_root) / 'config/market-recovery.json'
    if not path.exists():
        return primary
    config = json.loads(path.read_text(encoding='utf-8'))
    if not config.get('enabled'):
        return primary
    return PublicHistoryRecovery(primary, config, checkpoint_root=checkpoint_root, etfs=etfs)
