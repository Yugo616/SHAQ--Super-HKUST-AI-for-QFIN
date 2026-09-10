"""Immutable execution observations, never prediction evidence or daily labels."""
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from .hashing import sha256_payload
from .market_calendar import market_session
from .settings import _atomic_json

ET = ZoneInfo('America/New_York')
MINUTE_NAMESPACE = 'minute_observations_v1'


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Minute observations require timezone-aware timestamps')
    return stamp


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def target_bars(trade_date, records, symbols):
    session = market_session(date.fromisoformat(trade_date))
    if session is None:
        raise ValueError('Minute settlement requires a trading session')
    targets = dict(entry=session.market_open.replace(hour=9, minute=31),
                   exit=session.market_close - timedelta(minutes=5))
    output = {}
    for symbol in symbols:
        output[symbol] = {}
        for phase, target in targets.items():
            matches = []
            for row in records.get(symbol, []):
                try:
                    stamp = _stamp(row.get('timestamp'))
                except (ValueError, TypeError):
                    continue
                if stamp == target:
                    matches.append(row)
            row = matches[0] if len(matches) == 1 else None
            output[symbol][phase] = (dict(timestamp=target.isoformat(), open=row['open'],
                usable_volume=_finite(row.get('volume')) and row['volume'] > 0)
                if row and _finite(row.get('open')) and row['open'] > 0 else None)
    return output


class MinuteStore:
    """One content-addressed collection per date/provider/symbol set and read time."""
    def __init__(self, root: Path):
        self.root = root

    def observe(self, trade_date, symbols, records, *, provider, observed_at):
        day = date.fromisoformat(trade_date)
        now = _stamp(observed_at).astimezone(ET)
        session = market_session(day)
        if not session or now <= session.market_close:
            raise ValueError('Execution collection is post-close only')
        symbols = sorted(set(symbols))
        document = dict(schema_version=1, trade_date=trade_date, provider=provider,
            interval='1m', price_adjustment='unadjusted', session_scope='US_regular_session',
            timestamp_semantics='bar_start', captured_at_et=now.isoformat(), symbols=symbols,
            source='Yahoo Finance via YFinanceProvider.history' if provider == 'yfinance' else provider,
            records={symbol: records.get(symbol, []) for symbol in symbols})
        digest = sha256_payload(document)
        document['observation_sha256'] = digest
        collection = sha256_payload([trade_date, provider, symbols])
        destination = self.root / collection / 'observations' / (digest + '.json')
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / '.lock')):
            if destination.exists() and json.loads(destination.read_text()) != document:
                raise ValueError('Minute observation was modified')
            if not destination.exists():
                _atomic_json(destination, document)
        return self.snapshot(trade_date, symbols, provider=provider)

    def snapshot(self, trade_date, symbols, *, provider='yfinance'):
        symbols = sorted(set(symbols))
        if not symbols:
            return dict(status='not_required', trade_date=trade_date, symbols=[],
                        provider=provider, records={}, targets={}, observation_hashes=[],
                        correction=False, execution_sha256=sha256_payload({}), captured_at_et=None)
        observations = []
        for path in self.root.glob('*/observations/*.json'):
            document = json.loads(path.read_text())
            unsigned = {k: v for k, v in document.items() if k != 'observation_sha256'}
            if sha256_payload(unsigned) != path.stem or document.get('observation_sha256') != path.stem:
                raise ValueError('Minute observation hash mismatch')
            if (document['trade_date'] == trade_date and document['provider'] == provider
                    and set(symbols).issubset(document['symbols'])):
                observations.append(document)
        observations.sort(key=lambda row: (_stamp(row['captured_at_et']), row['observation_sha256']))
        base = dict(provider=provider, interval='1m', price_adjustment='unadjusted',
                    session_scope='US_regular_session', trade_date=trade_date, symbols=symbols,
                    observation_hashes=[row['observation_sha256'] for row in observations])
        if not observations:
            return dict(base, status='pending', targets={}, records={}, correction=False,
                        execution_sha256=None, captured_at_et=None)
        current = observations[-1]
        targets = target_bars(trade_date, current['records'], symbols)
        execution_hash = sha256_payload(targets)
        # Confirmation survives identical reads. A correction starts a new stable
        # sequence; observations before that revision cannot confirm the new value.
        stable = [current]
        correction = False
        for prior in reversed(observations[:-1]):
            if target_bars(trade_date, prior['records'], symbols) != targets:
                correction = True
                break
            stable.append(prior)
        earliest = _stamp(stable[-1]['captured_at_et']).astimezone(ET).date()
        confirmed = any((_stamp(row['captured_at_et']).astimezone(ET).date() > earliest
                         and _stamp(row['captured_at_et']).date() > date.fromisoformat(trade_date)
                         and market_session(_stamp(row['captured_at_et']).astimezone(ET).date()) is not None)
                        for row in stable)
        return dict(base, status='final' if confirmed else 'provisional', targets=targets,
                    records={symbol: current['records'][symbol] for symbol in symbols},
                    execution_sha256=execution_hash, correction=correction,
                    captured_at_et=current['captured_at_et'],
                    source=current['source'], confirmed_by_independent_reobservation=confirmed)


def refresh_minute_observations(*, research_root, rows, profile, observed_at=None, market_provider=None):
    """Background service entry point. Never called during dashboard rendering."""
    from .data_providers import YFinanceProvider
    now = (observed_at or datetime.now(ET)).astimezone(ET)
    store = MinuteStore(research_root / MINUTE_NAMESPACE)
    if profile.market_provider != 'yfinance':
        return {'refreshed_dates': [], 'failures': [], 'status': 'unavailable_provider'}
    grouped = {}
    for row in rows:
        day = date.fromisoformat(row['trade_date'])
        session = market_session(day)
        if session and now > session.market_close:
            grouped.setdefault(day, set()).update(p['symbol'] for p in row.get('predictions', []))
    provider = market_provider or YFinanceProvider(profile)
    refreshed, failures = [], []
    for day, symbols in sorted(grouped.items()):
        if not symbols:
            continue
        try:
            data = provider.history(sorted(symbols), start=day, end=day + timedelta(days=1),
                                    interval='1m', prepost=False)
            store.observe(day.isoformat(), sorted(symbols), data, provider='yfinance', observed_at=now)
            refreshed.append(day.isoformat())
        except Exception as exc:
            failures.append(dict(trade_date=day.isoformat(), error_type=type(exc).__name__, message=str(exc)))
    return {'refreshed_dates': refreshed, 'failures': failures}
