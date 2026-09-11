"""Immutable execution observations, never prediction evidence or daily labels."""
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from .hashing import sha256_payload
from .market_calendar import market_session, next_market_session
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

    def observe(self, trade_date, symbols, records, *, provider, observed_at,
                fresh_provider_read=True):
        day = date.fromisoformat(trade_date)
        now = _stamp(observed_at).astimezone(ET)
        session = market_session(day)
        if not session or now <= session.market_close:
            raise ValueError('Execution collection is post-close only')
        symbols = sorted(set(symbols))
        document = dict(schema_version=1, trade_date=trade_date, provider=provider,
            interval='1m', price_adjustment='unadjusted', session_scope='US_regular_session',
            timestamp_semantics='bar_start', captured_at_et=now.isoformat(), symbols=symbols,
            fresh_provider_read=bool(fresh_provider_read),
            source='Yahoo Finance via YFinanceProvider.history' if provider == 'yfinance' else provider,
            records=records)
        digest = sha256_payload(document)
        document['observation_sha256'] = digest
        collection = sha256_payload([trade_date, provider, symbols])
        destination = self.root / collection / 'observations' / (digest + '.json')
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / '.lock')):
            if destination.exists() and json.loads(destination.read_text(encoding="utf-8")) != document:
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
            document = json.loads(path.read_text(encoding="utf-8"))
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
        # Receipts retain the actual response. Missing targets are unavailable
        # retrievals, not revisions, and never re-date or confirm earlier evidence.
        evidence = {symbol: dict(entry=None, exit=None) for symbol in symbols}
        correction = False
        for observation in observations:
            observed_targets = target_bars(trade_date, observation['records'], symbols)
            captured = observation['captured_at_et']
            captured_time = _stamp(captured).astimezone(ET)
            for symbol, phases in observed_targets.items():
                for phase, target in phases.items():
                    if target is None:
                        continue
                    prior = evidence[symbol][phase]
                    if prior is None or prior['target'] != target:
                        correction = correction or prior is not None
                        prior = dict(target=target, first_captured_at_et=captured, confirmed=False)
                    elif (captured_time > _stamp(prior['first_captured_at_et']).astimezone(ET)
                          and (observation.get('fresh_provider_read') is True
                               or ('fresh_provider_read' not in observation
                                   and captured_time.date() > _stamp(
                                       prior['first_captured_at_et']).astimezone(ET).date()))):
                        prior['confirmed'] = True
                    prior.update(captured_at_et=captured,
                                 observation_sha256=observation['observation_sha256'])
                    evidence[symbol][phase] = prior
        targets = {symbol: {phase: value['target'] if value else None
                           for phase, value in phases.items()} for symbol, phases in evidence.items()}
        used = [value for phases in evidence.values() for value in phases.values() if value]
        confirmed = len(used) == len(symbols) * 2 and all(value['confirmed'] for value in used)
        current = observations[-1]
        missing = {symbol: [phase for phase, value in phases.items() if value is None]
                   for symbol, phases in observed_targets.items() if any(value is None for value in phases.values())}
        refresh = dict(status='unavailable' if sum(map(len, missing.values())) == len(symbols) * 2
                       else 'partial_unavailable' if missing else 'available',
                       captured_at_et=current['captured_at_et'],
                       observation_sha256=current['observation_sha256'], missing_targets=missing)
        # This is a derived execution view, never a newly captured observation.
        # Each retained target explicitly points at its own actual source receipt.
        records = {symbol: [dict(timestamp=value['target']['timestamp'], open=value['target']['open'],
                                volume=1 if value['target']['usable_volume'] else 0)
                            for value in phases.values() if value] for symbol, phases in evidence.items()}
        return dict(base, status='final' if confirmed else 'provisional', targets=targets,
                    records=records, execution_sha256=sha256_payload(targets), correction=correction,
                    captured_at_et=max((value['captured_at_et'] for value in used), key=_stamp) if used else None,
                    latest_refresh=refresh,
                    target_observations={symbol: {phase: {key: item for key, item in value.items() if key != 'target'}
                                                 if value else None for phase, value in phases.items()}
                                         for symbol, phases in evidence.items()},
                    source=current['source'], confirmed_by_independent_reobservation=confirmed)


def refresh_minute_observations(*, research_root, rows, profile, observed_at=None, market_provider=None,
                                eligible_dates=None):
    """Background service entry point. Never called during dashboard rendering."""
    from .data_providers import YFinanceProvider
    now = (observed_at or datetime.now(ET)).astimezone(ET)
    store = MinuteStore(research_root / MINUTE_NAMESPACE)
    if profile.market_provider != 'yfinance':
        return {'refreshed_dates': [], 'failures': [], 'status': 'unavailable_provider'}
    grouped = {}
    for row in rows:
        day = date.fromisoformat(row['trade_date'])
        if eligible_dates is not None and row['trade_date'] not in eligible_dates:
            continue
        session = market_session(day)
        if session and now > session.market_close:
            grouped.setdefault(day, set()).update(p['symbol'] for p in row.get('predictions', []))
    provider = market_provider or YFinanceProvider(profile)
    refreshed, failures = [], []
    for day, symbols in sorted(grouped.items()):
        if not symbols:
            continue
        try:
            history = getattr(provider, 'fresh_history', provider.history)
            data = history(sorted(symbols), start=day, end=day + timedelta(days=1),
                           interval='1m', prepost=False)
            fresh = hasattr(provider, 'fresh_history') or getattr(
                provider, 'last_history_was_fresh', True)
            snapshot = store.observe(day.isoformat(), sorted(symbols), data, provider='yfinance',
                                     observed_at=now, fresh_provider_read=fresh)
            receipt = snapshot['latest_refresh']
            if receipt['status'] != 'available':
                failures.append(dict(trade_date=day.isoformat(), error_type='UnavailableMinuteTargets',
                    message='Refresh target minutes unavailable; earlier evidence is not a new observation.',
                    **receipt))
            else:
                refreshed.append(day.isoformat())
        except Exception as exc:
            failures.append(dict(trade_date=day.isoformat(), error_type=type(exc).__name__, message=str(exc)))
    return {'refreshed_dates': refreshed, 'failures': failures}


RETRY_MINUTES = (5, 15, 30, 60)


def load_settlement_attempts(research_root):
    path = Path(research_root) / 'minute_refresh_attempts.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record_settlement_attempt(research_root, dates, now, *, app_open=False):
    now = _stamp(now).astimezone(ET)
    attempts = load_settlement_attempts(research_root)
    for day_text in dates:
        item = attempts.setdefault(day_text, {'scheduled_offsets': []})
        session = market_session(date.fromisoformat(day_text))
        elapsed = (now - session.market_close).total_seconds() / 60 if session else 0
        used = set(item['scheduled_offsets'])
        offset = next((value for value in RETRY_MINUTES if elapsed >= value and value not in used), None)
        if elapsed <= RETRY_MINUTES[-1] + 1 and offset in RETRY_MINUTES and offset not in item['scheduled_offsets']:
            item['scheduled_offsets'].append(offset)
            item['scheduled_offsets'].sort()
        if app_open:
            item['last_app_open_date'] = now.date().isoformat()
        if session and now >= next_market_session(date.fromisoformat(day_text)).market_close + timedelta(minutes=5):
            item['confirmation_attempted_at_et'] = now.isoformat()
        item['last_attempted_at_et'] = now.isoformat()
    _atomic_json(Path(research_root) / 'minute_refresh_attempts.json', attempts)
    return attempts


def settlement_due_dates(rows, now, attempts=None, *, app_open=False, manual=False):
    """Pure local due-state check. Calling it never reads credentials or the network."""
    now = _stamp(now).astimezone(ET)
    attempts = attempts or {}
    due = []
    by_date = {}
    for row in rows:
        by_date.setdefault(row['trade_date'], []).append(row)
    for day_text, day_rows in sorted(by_date.items()):
        session = market_session(date.fromisoformat(day_text))
        if not session or now < session.market_close + timedelta(minutes=5):
            continue
        reviewed = all(row.get('minute', {}).get('status') == 'final' and
                       all(value.get('status') == 'final' for value in row.get('labels', {}).values())
                       for row in day_rows)
        if reviewed:
            continue
        if manual:
            due.append(day_text); continue
        complete_provisional = all(
            row.get('minute', {}).get('status') == 'provisional'
            and all(value.get('status') in ('provisional', 'final')
                    for value in row.get('labels', {}).values())
            for row in day_rows)
        elapsed = (now - session.market_close).total_seconds() / 60
        used = set(attempts.get(day_text, {}).get('scheduled_offsets', []))
        offset = next((value for value in RETRY_MINUTES if elapsed >= value and value not in used), None)
        if not complete_provisional and offset is not None and elapsed <= RETRY_MINUTES[-1] + 1:
            due.append(day_text); continue
        next_session = next_market_session(date.fromisoformat(day_text))
        confirmation_due = next_session.market_close + timedelta(minutes=5)
        local_day = now.date().isoformat()
        if now >= confirmation_due and not attempts.get(day_text, {}).get('confirmation_attempted_at_et'):
            due.append(day_text); continue
        if app_open and now >= confirmation_due and attempts.get(day_text, {}).get('last_app_open_date') != local_day:
            due.append(day_text)
    return due
