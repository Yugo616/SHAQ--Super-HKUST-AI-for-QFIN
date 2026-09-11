"""Brokerless minute accounts and read-only persisted legacy settlements."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from .hashing import sha256_payload
from .market_calendar import market_session
from .settings import _atomic_json

ET = ZoneInfo('America/New_York')
ENGINE_ID = 'zipline-reloaded'
ENGINE_VERSION = '3.1.1'
POLICY_ID = 'rth-minute-open-0931-close-minus5-v1'
ACCOUNT_NAMESPACE = 'zipline_minute_v2'


@dataclass(frozen=True)
class AccountRules:
    initial_cash: float = 10000
    per_prediction_budget: float = 1000
    commission_rate: float = 0.0005
    slippage_rate: float = 0.0005
    schema_version: int = 2
    risk_fraction: float | None = None
    lookback: int | None = None
    per_symbol_cap: float | None = None
    gross_cap: float | None = None

    def __post_init__(self):
        if self.schema_version != 2:
            raise ValueError('Only the minute-account schema is executable; legacy is read-only')
        for name in ('initial_cash', 'per_prediction_budget', 'commission_rate', 'slippage_rate'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid account rule: ' + name)
        if self.initial_cash <= 0 or self.per_prediction_budget <= 0 or self.slippage_rate >= 1:
            raise ValueError('Invalid cash, budget or slippage')
        optional = ('risk_fraction', 'per_symbol_cap', 'gross_cap')
        if any(getattr(self, name) is not None and (not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0)
               for name in optional):
            raise ValueError('Invalid risk sizing rule')
        if self.lookback is not None and (isinstance(self.lookback, bool) or self.lookback < 2):
            raise ValueError('Risk lookback must be at least two trading days')


def experimental_risk_rules(**changes):
    """Approved experimental defaults; they are not claimed to be optimal."""
    values = dict(risk_fraction=.002, lookback=20, per_symbol_cap=.10, gross_cap=.30)
    values.update(changes)
    return AccountRules(**values)


def _rules_dict(rules):
    return {key: value for key, value in asdict(rules).items() if value is not None}


def freeze_risk_sizing(predictions, history, *, trade_date, frozen_at_et, lookback=20):
    """Attach point-in-time volatility evidence to a new prediction copy."""
    _time(frozen_at_et)
    output = []
    for prediction in predictions:
        by_date = {}
        for row in history.get(prediction['symbol'], []):
            day_text = str(row.get('date') or row.get('timestamp') or '')[:10]
            if not day_text or day_text >= trade_date:
                continue
            opening, closing = row.get('open'), row.get('close')
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value > 0 for value in (opening, closing)):
                value = dict(date=day_text, open=opening, close=closing,
                             return_value=closing / opening - 1)
                if day_text in by_date and by_date[day_text] != value:
                    by_date[day_text] = None
                else:
                    by_date[day_text] = value
        eligible = [value for value in by_date.values() if value is not None]
        eligible = sorted(eligible, key=lambda row: row['date'])[-lookback:]
        sizing = dict(lookback=lookback, observation_count=len(eligible),
                      latest_observation_date=eligible[-1]['date'] if eligible else None,
                      frozen_at_et=frozen_at_et, price_basis='unadjusted_same_day_close/open-1')
        if len(eligible) == lookback:
            mean = math.fsum(row['return_value'] for row in eligible) / lookback
            sizing['sigma'] = math.sqrt(math.fsum((row['return_value'] - mean) ** 2
                                                  for row in eligible) / (lookback - 1))
        else:
            sizing['sigma'] = None
            sizing['unavailable_reason'] = 'insufficient_prior_trading_days'
        sizing['inputs_sha256'] = sha256_payload(eligible)
        output.append({**prediction, 'risk_sizing': sizing})
    return output


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Official unadjusted prices must be finite and positive')
    return Decimal(str(value))


def replay_day(trade_date, predictions, labels, rules: AccountRules, *, cash=None, minute=None):
    """Execute frozen directions only against dedicated minute observations."""
    from .minute_execution import Bar, Rules, SessionInput, Signal, run_session
    from .minute_settlements import target_bars
    import pandas as pd

    session = market_session(date.fromisoformat(trade_date))
    if session is None:
        raise ValueError('Cannot replay a non-trading day')
    cash = float(rules.initial_cash if cash is None else cash)
    if not math.isfinite(cash):
        raise ValueError('Invalid account balance')
    predictions = sorted(predictions, key=lambda p: p['symbol'])
    symbols = [p['symbol'] for p in predictions]
    if len(set(symbols)) != len(symbols) or any(p['direction'] not in ('bullish', 'bearish') for p in predictions):
        raise ValueError('Duplicate symbols or invalid directions')
    for label in labels.values():
        if label.get('status') in ('provisional', 'final'):
            _positive(label.get('official_unadjusted_open'))
            _positive(label.get('official_unadjusted_close'))
    result = dict(engine=ENGINE_ID, engine_version=ENGINE_VERSION, policy_id=POLICY_ID,
                  trade_date=trade_date, opening_cash=cash, closing_cash=cash,
                  status='empty', orders=[], trades=[], closing_positions={},
                  net_pnl=0.0, gross_pnl=0.0, fees=0.0, slippage_cost=0.0,
                  opening_capital_used=0.0, zero_cost=None, legacy=False,
                  entry_reference_at_et=(session.market_open + timedelta(minutes=1)).isoformat(),
                  exit_reference_at_et=(session.market_close - timedelta(minutes=5)).isoformat(),
                  price_basis='unadjusted_RTH_1m_target_open',
                  official_label_basis='unadjusted_regular_session_open_to_close')
    if not predictions:
        return result
    minute = minute or {}
    if not minute.get('records'):
        return dict(result, status='pending', closing_cash=None, net_pnl=None, gross_pnl=None,
                    data_status=minute.get('status', 'pending'))
    targets = target_bars(trade_date, minute['records'], symbols)
    bars = {}
    for symbol, phases in targets.items():
        for target in phases.values():
            if target is not None:
                # Only contractual opens and volume eligibility reach the engine.
                price = target['open']
                bars[symbol, pd.Timestamp(target['timestamp'])] = Bar(
                    price, price, price, price, 1.0 if target['usable_volume'] else 0.0)
    ticket_budgets = None
    unavailable = {}
    if rules.risk_fraction is not None:
        ticket_budgets = {}
        gross_remaining = cash * rules.gross_cap
        for prediction in predictions:
            sizing = prediction.get('risk_sizing') or {}
            sigma = sizing.get('sigma')
            reason = None
            if not isinstance(sigma, (int, float)) or isinstance(sigma, bool) or not math.isfinite(sigma) or sigma <= 0:
                reason = 'missing_frozen_sigma'
            elif sizing.get('lookback') != rules.lookback:
                reason = 'invalid_frozen_lookback'
            elif not sizing.get('inputs_sha256') or not sizing.get('frozen_at_et'):
                reason = 'missing_frozen_sizing_provenance'
            elif sizing.get('latest_observation_date', trade_date) >= trade_date:
                reason = 'future_sizing_data_rejected'
            budget = 0. if reason else min(cash * rules.risk_fraction / sigma,
                                           cash * rules.per_symbol_cap, gross_remaining)
            ticket_budgets[prediction['symbol']] = max(0., budget)
            gross_remaining -= ticket_budgets[prediction['symbol']]
            if reason:
                unavailable[prediction['symbol']] = reason
    engine = run_session(SessionInput(trade_date, tuple(
        Signal(p['symbol'], 1 if p['direction'] == 'bullish' else -1)
        for p in predictions), bars, ticket_budgets=ticket_budgets),
        Rules(initial_cash=max(0., cash), ticket_budget=rules.per_prediction_budget,
              commission=rules.commission_rate, slippage=rules.slippage_rate))
    if not engine['reconciled'] or not engine['zero_cost']['reconciled']:
        raise ValueError('Zipline engine cash or fees failed independent reconciliation')
    cash_offset = min(0., cash)
    for fill in engine['fills']:
        phase = 'open' if fill['order_id'].startswith('entry:') else 'close'
        result['orders'].append(dict(fill, phase=phase, status='Completed',
            size=fill['shares'], reference_at_et=pd.Timestamp(fill['bar_start']).tz_convert(ET).isoformat(),
            executed_at_et=pd.Timestamp(fill['engine_bar_end']).tz_convert(ET).isoformat()))
    for prediction in predictions:
        symbol, direction = prediction['symbol'], prediction['direction']
        fills = [f for f in engine['fills'] if f['symbol'] == symbol]
        entry = next((f for f in fills if f['order_id'].startswith('entry:')), None)
        exit_fill = next((f for f in fills if f['order_id'].startswith('exit:')), None)
        quantity = abs(entry['shares']) if entry else 0
        sign = 1 if direction == 'bullish' else -1
        label = labels.get(symbol, {})
        official_open = label.get('official_unadjusted_open') if label.get('status') in ('provisional', 'final') else None
        official_close = label.get('official_unadjusted_close') if label.get('status') in ('provisional', 'final') else None
        gross = sign * quantity * (exit_fill['reference_open'] - entry['reference_open']) if exit_fill else (None if entry else 0.)
        fees = math.fsum(f['commission'] for f in fills)
        slippage = math.fsum(abs(f['shares']) * f['reference_open'] * rules.slippage_rate for f in fills)
        result['trades'].append(dict(symbol=symbol, direction=direction, quantity=quantity,
            status='closed' if exit_fill else 'open_incomplete' if entry else
                   'unavailable_entry' if targets[symbol]['entry'] is None else
                   'unfilled_volume' if not targets[symbol]['entry']['usable_volume'] else 'unfilled_budget',
            budget=(ticket_budgets or {}).get(symbol, min(rules.per_prediction_budget, max(0., cash)/len(symbols))),
            sizing_unavailable_reason=unavailable.get(symbol), risk_sizing=prediction.get('risk_sizing'),
            official_open=official_open, official_close=official_close,
            official_open_to_close_return=(official_close / official_open - 1)
                if official_open is not None and official_close is not None else None,
            direction_adjusted_return=(sign * (official_close / official_open - 1))
                if official_open is not None and official_close is not None else None,
            direction_correct=sign*(official_close-official_open)>0 if official_open is not None and official_close is not None else None,
            entry_reference_open=entry['reference_open'] if entry else None,
            exit_reference_open=exit_fill['reference_open'] if exit_fill else None,
            entry_price=entry['price'] if entry else None, exit_price=exit_fill['price'] if exit_fill else None,
            entry_reference_at_et=result['entry_reference_at_et'],
            exit_reference_at_et=result['exit_reference_at_et'],
            gross_pnl=gross, fees=fees, slippage_cost=slippage,
            net_pnl=gross-slippage-fees if gross is not None else None))
    status = ('incomplete' if engine['positions'] else
              'unavailable' if any(targets[s]['entry'] is None for s in symbols) else
              'final' if minute.get('status') == 'final' else 'provisional')
    result.update(status=status, closing_cash=engine['final_cash']+cash_offset,
        closing_positions=engine['positions'], net_pnl=engine['net_pnl'], gross_pnl=engine['gross_pnl'],
        fees=engine['commissions'], slippage_cost=engine['slippage_cost'],
        opening_capital_used=engine['reserved_collateral'], zero_cost=engine['zero_cost'],
        engine_identity=engine['engine'], data_status=minute.get('status', 'pending'),
        observation_hashes=minute.get('observation_hashes', []),
        execution_sha256=minute.get('execution_sha256'), correction=minute.get('correction', False),
        captured_at_et=minute.get('captured_at_et'),
        latest_refresh=minute.get('latest_refresh'), target_observations=minute.get('target_observations', {}),
        source=minute.get('source', 'dedicated minute observations'), unfilled=engine['unfilled'])
    return result


def _time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('Completion time must include a timezone')
    return stamp


def _completion(row):
    try:
        return _time(row.get('completed_at_et'))
    except (ValueError, TypeError):
        return None


def _late(row):
    completion = _completion(row)
    if row.get('cutoff_status') == 'late_research_only':
        return True
    # This is the existing research publication deadline, not an execution time.
    deadline = _time(row['publication_deadline_et']) if row.get('publication_deadline_et') else None
    if deadline is None and completion:
        deadline = datetime.combine(date.fromisoformat(row['trade_date']), datetime.min.time(), ET).replace(hour=9)
    return bool(completion and deadline and completion > deadline)


class AccountStore:
    """Immutable settlement revisions; summary is reproducible from source rows."""
    def __init__(self, root: Path):
        self.legacy_root = root
        self.root = root / ACCOUNT_NAMESPACE

    def activate(self, rules: AccountRules, activated_at=None, *, continuity=False):
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / '.lock')):
            path = self.root / 'activation.json'
            if path.exists():
                prior = json.loads(path.read_text(encoding="utf-8"))
                if prior['rules'] == _rules_dict(rules):
                    return prior
                if activated_at and _time(activated_at) <= _time(prior['activated_at']):
                    raise ValueError('A changed account policy must activate after its predecessor')
            predecessor = sha256_payload(prior) if path.exists() else None
            value = {'engine': ENGINE_ID, 'engine_version': ENGINE_VERSION, 'policy_id': POLICY_ID,
                     'rules': _rules_dict(rules), 'activated_at': activated_at or datetime.now(ET).isoformat(),
                     'continuity': bool(continuity), 'predecessor_activation_sha256': predecessor}
            _time(value['activated_at'])
            archive = self.root / 'policies' / (sha256_payload(value) + '.json')
            _atomic_json(archive, value)
            _atomic_json(path, value)
            return value

    def refresh(self, rows):
        path = self.root / 'activation.json'
        if not path.exists():
            return dict(accounts=[], results=[], rules=None, legacy=self.read_legacy())
        current = json.loads(path.read_text(encoding="utf-8"))
        policies = sorted([json.loads(p.read_text(encoding="utf-8")) for p in (self.root / 'policies').glob('*.json')],
                          key=lambda p: _time(p['activated_at']))
        result = self._refresh_policy(rows, current)
        for i, policy in enumerate(policies):
            if current.get('continuity'):
                break
            if _time(policy['activated_at']) >= _time(current['activated_at']):
                continue
            end = _time(policies[i+1]['activated_at']) if i+1 < len(policies) else _time(current['activated_at'])
            period_rows = [r for r in rows if _completion(r) is not None
                           and _time(policy['activated_at']) <= _completion(r) < end and r.get('source_eligible') is True]
            old = self._refresh_policy(period_rows, policy)
            result['accounts'].extend(old['accounts'])
            result['results'].extend(old['results'])
        result['accounts'].sort(key=lambda a: a['account_id'])
        result['results'].sort(key=lambda r: (r['trade_date'], r['batch_id'], r['variant_key'], r['account_id']))
        result['legacy'] = self.read_legacy()
        result.update(engine=ENGINE_ID, engine_version=ENGINE_VERSION, policy_id=POLICY_ID)
        summary = dict(result=result, row_hashes={self._row_key(row): sha256_payload(row) for row in rows})
        summary['summary_sha256'] = sha256_payload(summary)
        _atomic_json(self.root / 'summary.json', summary)
        return result

    @staticmethod
    def _row_key(row):
        return sha256_payload([row['batch_id'], row['variant_key'], row['series_key']])

    def saved_settlement_for(self, row, *, exclude_rules_hash=None, rules_hash=None):
        matches = []
        for path in (self.root / 'settlements').glob('*.json'):
            value = json.loads(path.read_text(encoding='utf-8'))
            if sha256_payload(value) != path.stem:
                raise ValueError('Settlement revision was modified')
            if (value.get('source_result_hash') == row.get('variant_result_sha256')
                    and value.get('rules_hash') != exclude_rules_hash
                    and (rules_hash is None or value.get('rules_hash') == rules_hash)
                    and value.get('labels_hash') == sha256_payload(row.get('labels', {}))
                    and value.get('minute_hash') == sha256_payload(row.get('minute', {}))
                    and value.get('status') in ('final', 'provisional', 'empty')):
                matches.append((path.stem, value))
        return sorted(matches, key=lambda pair: pair[0])[-1] if matches else None

    def view(self, rows):
        """Read cached aggregation only. Background reconciliation owns engine work."""
        path = self.root / 'summary.json'
        cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if cached:
            unsigned = {k: v for k, v in cached.items() if k != 'summary_sha256'}
            if sha256_payload(unsigned) != cached.get('summary_sha256'):
                raise ValueError('Account summary hash mismatch')
        activation_path = self.root / 'activation.json'
        policy = json.loads(activation_path.read_text(encoding="utf-8")) if activation_path.exists() else {}
        if cached and cached.get('result', {}).get('rules_hash') != sha256_payload(policy):
            cached = {}
        result = cached.get('result', dict(accounts=[], results=[], rules=policy.get('rules'),
                    activated_at=policy.get('activated_at'), engine=ENGINE_ID,
                    engine_version=ENGINE_VERSION, policy_id=POLICY_ID, legacy=self.read_legacy()))
        pending = []
        for row in rows:
            if cached.get('row_hashes', {}).get(self._row_key(row)) == sha256_payload(row):
                continue
            scope = ('late' if _late(row) else 'practice' if row.get('source_eligible') is not True else 'historical'
                     if _completion(row) is None or not policy or
                     _completion(row) < _time(policy['activated_at']) else 'forward')
            pending.append(dict(batch_id=row['batch_id'], variant_key=row['variant_key'],
                trade_date=row['trade_date'], label=row['label'], model=row.get('model'),
                scope=scope, source_scope=scope, status='pending', trades=[], orders=[],
                engine=ENGINE_ID, engine_version=ENGINE_VERSION, policy_id=POLICY_ID,
                legacy=False, reconciliation_pending=True))
        pending_keys = {(r['batch_id'], r['variant_key']) for r in pending}
        result['results'] = [r for r in result['results'] if (r['batch_id'], r['variant_key']) not in pending_keys] + pending
        result['reconciliation_pending'] = bool(pending)
        return result

    def _refresh_policy(self, rows, policy):
        with FileLock(str(self.root / '.lock')):
            rules = AccountRules(**policy['rules'])
            rules_hash = sha256_payload(policy)
            ordered = sorted(rows, key=lambda r: (r['trade_date'], _completion(r) or datetime.max.replace(tzinfo=ET), r['batch_id'], r['variant_key']))
            accounts, results, selected = {}, [], set()
            for row in ordered:
                key = (row['trade_date'], row['series_key'])
                completion = _completion(row)
                session = market_session(date.fromisoformat(row['trade_date']))
                source_eligible = row.get('source_eligible') is True
                late = _late(row)
                if late or (completion and (not session or completion.astimezone(ET).date() != session.session_date or completion >= session.market_open)):
                    source_eligible = False
                duplicate = source_eligible and key in selected
                if source_eligible:
                    selected.add(key)
                scope = 'duplicate' if duplicate else 'late' if late or (row.get('source_eligible') is True and not source_eligible) else 'practice' if not source_eligible else 'historical' if _completion(row) is None or _completion(row) < _time(policy['activated_at']) else 'forward'
                continuity_key = [row.get('method_identity', row['series_key']),
                                  row.get('model_identity', row.get('model'))]
                identity = sha256_payload([continuity_key if policy.get('continuity') else row['series_key'],
                                           ENGINE_ID, ENGINE_VERSION, POLICY_ID, rules_hash])
                base = dict(batch_id=row['batch_id'], variant_key=row['variant_key'], trade_date=row['trade_date'],
                            label=row['label'], model=row.get('model'), scope=scope, source_scope=scope,
                            series_key=row['series_key'], method_identity=row.get('method_identity', row['series_key']),
                            model_identity=row.get('model_identity', row.get('model')),
                            account_id=identity, rules=_rules_dict(rules), policy_id=POLICY_ID, legacy=False,
                            engine=ENGINE_ID, engine_version=ENGINE_VERSION)
                if duplicate:
                    results.append(dict(base, status='duplicate', trades=[], orders=[]))
                    continue
                account = None
                if scope in ('forward', 'historical'):
                    historical_net = math.fsum(
                        float(item.get('net_pnl') or 0) for item in results
                        if item.get('account_id') == identity and item.get('scope') == 'historical'
                        and item.get('status') in ('final', 'provisional', 'empty')) if policy.get('continuity') else 0.
                    opening_equity = rules.initial_cash + historical_net
                    funded = accounts.setdefault(identity, dict(account_id=identity, series_key=row['series_key'],
                        label=row['label'], model=row.get('model'), engine=ENGINE_ID, engine_version=ENGINE_VERSION,
                        policy_id=POLICY_ID, method_identity=row.get('method_identity', row['series_key']),
                        model_identity=row.get('model_identity', row.get('model')), legacy=False, equity=opening_equity, gross_equity=opening_equity,
                        rules=_rules_dict(rules), activated_at=policy['activated_at'],
                        opening_simulation_balance=opening_equity, source_scope='simulation_cumulative' if policy.get('continuity') else 'forward',
                        peak=opening_equity, max_drawdown=0.0, fees=0.0, slippage_cost=0.0, sessions=0, curve=[], blocked=False))
                    if scope == 'forward':
                        if policy.get('continuity') and not funded['sessions'] and not funded['curve']:
                            funded.update(equity=opening_equity, gross_equity=opening_equity,
                                          opening_simulation_balance=opening_equity, peak=opening_equity)
                        account = funded
                if account and account['blocked']:
                    results.append(dict(base, status='blocked_previous', trades=[], orders=[]))
                    continue
                cash = account['equity'] if account else rules.initial_cash
                processing_started = datetime.now(ET).isoformat()
                saved = (self.saved_settlement_for(row, exclude_rules_hash=rules_hash)
                         if scope == 'historical' and policy.get('continuity') else None)
                if saved:
                    source_hash, source_document = saved
                    replay = {key: value for key, value in source_document.items()
                              if key not in base and key not in ('settlement_hash', 'rules_hash')}
                    replay['source_settlement_hash'] = source_hash
                    replay['replayed'] = False
                else:
                    try:
                        replay_rules = rules if scope == 'forward' else replace(
                            rules, risk_fraction=None, lookback=None, per_symbol_cap=None, gross_cap=None)
                        replay = replay_day(row['trade_date'], row['predictions'], row['labels'], replay_rules,
                                            cash=cash, minute=row.get('minute'))
                    except (ValueError, RuntimeError) as exc:
                        replay = {'status': 'error', 'error': str(exc), 'orders': [], 'trades': []}
                entry = dict(base, **replay)
                if policy.get('continuity') and scope == 'forward':
                    entry['source_scope'] = 'simulation_cumulative'
                if account:
                    if replay['status'] in ('final', 'provisional', 'empty'):
                        account['equity'] = replay['closing_cash']
                        account['gross_equity'] += replay['gross_pnl']
                        account['fees'] += replay['fees']
                        account['slippage_cost'] += replay['slippage_cost']
                        account['sessions'] += 1
                        account['peak'] = max(account['peak'], account['equity'])
                        account['max_drawdown'] = max(account['max_drawdown'], 1 - account['equity'] / account['peak'])
                        account['curve'].append(dict(date=row['trade_date'], equity=account['equity'], gross_equity=account['gross_equity']))
                    else:
                        account['blocked'] = True
                entry['account_equity'] = account['equity'] if account else None
                reference_balance = (
                    account['equity'] if account else replay.get('closing_cash')
                    if scope == 'historical' and replay.get('status') in ('final', 'provisional', 'empty')
                    else None
                )
                entry['account_balance'] = reference_balance
                entry['account_cumulative_net_pnl'] = (
                    reference_balance - rules.initial_cash if reference_balance is not None else None
                )
                if replay['status'] in ('final', 'empty', 'provisional', 'incomplete', 'unavailable'):
                    document = dict(entry, rules_hash=rules_hash, source_result_hash=row['variant_result_sha256'], labels_hash=sha256_payload(row['labels']),
                                    minute_hash=sha256_payload(row.get('minute', {})))
                    document_hash = sha256_payload(document)
                    dest = self.root / 'settlements' / (document_hash + '.json')
                    if not dest.exists():
                        _atomic_json(dest, document)
                    elif json.loads(dest.read_text(encoding="utf-8")) != document:
                        raise ValueError('Settlement revision was modified')
                    entry['settlement_hash'] = document_hash
                    receipt_path = self.root / 'processing' / (document_hash + '.json')
                    if not receipt_path.exists():
                        _atomic_json(receipt_path, dict(settlement_hash=document_hash,
                            processing_started_at_et=processing_started,
                            processed_at_et=datetime.now(ET).isoformat()))
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if receipt.get('settlement_hash') != document_hash:
                        raise ValueError('Settlement processing receipt identity mismatch')
                    _time(receipt['processing_started_at_et'])
                    _time(receipt['processed_at_et'])
                    entry.update(processing_started_at_et=receipt['processing_started_at_et'],
                                 processed_at_et=receipt['processed_at_et'])
                results.append(entry)
            if policy.get('continuity'):
                for account in accounts.values():
                    if account['sessions'] or account['curve']:
                        continue
                    seed = rules.initial_cash + math.fsum(
                        float(item.get('net_pnl') or 0) for item in results
                        if item.get('account_id') == account['account_id']
                        and item.get('scope') == 'historical'
                        and item.get('status') in ('final', 'provisional', 'empty'))
                    account.update(equity=seed, gross_equity=seed,
                                   opening_simulation_balance=seed, peak=seed)
                for item in results:
                    if item.get('account_id') in accounts and item.get('status') == 'pending':
                        item['source_scope'] = 'simulation_cumulative'
                        item['account_balance'] = accounts[item['account_id']]['equity']
                        item['account_cumulative_net_pnl'] = item['account_balance'] - rules.initial_cash
            return dict(rules=_rules_dict(rules), rules_hash=rules_hash, activated_at=policy['activated_at'],
                        accounts=sorted(accounts.values(), key=lambda r: r['account_id']), results=results)

    def read_legacy(self):
        """Read saved v1 outputs only; missing legacy settlement is not replayed."""
        documents = []
        for path in sorted((self.legacy_root / 'settlements').glob('*.json')):
            value = json.loads(path.read_text(encoding="utf-8"))
            if sha256_payload(value) != path.stem:
                raise ValueError('Legacy settlement hash mismatch')
            documents.append(dict(value, settlement_hash=path.stem, legacy=True,
                                  source_scope='legacy_saved', correction=False))
        return dict(status='saved_only' if documents else 'unavailable',
                    engine='backtrader', read_only=True, results=documents,
                    missing_settlement_policy='unavailable_no_recalculation')


def reconcile_activation(store, rows, *, activated_at, apply=False):
    """One reviewed controller entrypoint. Preview performs no filesystem writes."""
    rules = experimental_risk_rules()
    sources = []
    for row in rows:
        saved = store.saved_settlement_for(row)
        if saved:
            digest, document = saved
            sources.append({'trade_date': row['trade_date'], 'batch_id': row['batch_id'],
                            'variant_key': row['variant_key'], 'source_settlement_hash': digest,
                            'source_scope': document.get('scope'), 'net_pnl': document.get('net_pnl')})
    preview = {'applied': False, 'activated_at': activated_at, 'rules': _rules_dict(rules),
               'row_count': len(rows), 'mode': 'simulation_cumulative',
               'source_settlements': sources,
               'projected_seed_by_method_model': {},
               'warning': 'Experimental defaults are not validated optimal.'}
    for row in rows:
        key = sha256_payload([row.get('method_identity', row['series_key']),
                              row.get('model_identity', row.get('model'))])
        matching = [item for item in sources if item['batch_id'] == row['batch_id']
                    and item['variant_key'] == row['variant_key']]
        if matching and matching[0]['net_pnl'] is not None:
            preview['projected_seed_by_method_model'][key] = (
                preview['projected_seed_by_method_model'].get(key, rules.initial_cash)
                + float(matching[0]['net_pnl']))
    if not apply:
        return preview
    activation = store.activate(rules, activated_at, continuity=True)
    result = store.refresh(rows)
    return {**preview, 'applied': True, 'activation': activation, 'result': result}
