"""Optional local display projection; never writes settlements or score eligibility."""
from copy import deepcopy
from datetime import datetime, timezone
import math
from .hashing import sha256_payload


def project_saved_balances(saved, daily_rows):
    """Sum saved daily PnL once per method, including late research records.

    Not an account replay: quantities, fills and costs remain unchanged. The
    selected pre-open decision wins. Legacy all-history preview falls back to
    its first saved run only when no eligible pre-open decision exists.
    """
    view = deepcopy(saved)
    initial = saved['rules']['initial_cash']
    if not isinstance(initial, (int, float)) or not math.isfinite(initial):
        raise ValueError('Invalid initial balance')
    metadata = {(r['batch_id'], r['variant_key']): r for r in daily_rows}
    rows = view.get('results', [])

    def order(row):
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        try:
            stamp = datetime.fromisoformat(info.get('completed_at_et', ''))
            if stamp.tzinfo is None:
                raise ValueError('Completion time must include timezone')
            stamp = stamp.astimezone(timezone.utc)
        except (ValueError, TypeError):
            stamp = datetime.max.replace(tzinfo=timezone.utc)
        return row['trade_date'], stamp, row['batch_id'], row['variant_key']

    def method_for(row):
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        return (row.get('method_identity') or info.get('method_identity')
                or row.get('series_key') or info.get('series_key') or row['variant_key'])

    def series_for(row):
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        model = (row.get('account_model_identity') or row.get('model_identity')
                 or info.get('account_model_identity') or info.get('model_identity'))
        method = method_for(row)
        return sha256_payload([method, model]) if model else method

    preferred = {}
    for row in rows:
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        if info.get('score_eligible') is True:
            key = series_for(row), row['trade_date']
            if key not in preferred or order(row) > order(preferred[key]):
                preferred[key] = row
    accounts, chosen = {}, set()
    for row in sorted(rows, key=order):
        row.update(balance_preview_counted=False, account_balance=None,
                   account_cumulative_net_pnl=None)
        if row.get('status') == 'duplicate':
            continue
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        method = method_for(row)
        series = series_for(row)
        account_id = 'local-preview:' + series
        row['account_id'] = account_id
        key = series, row['trade_date']
        selected = preferred.get(key)
        if selected and (row['batch_id'], row['variant_key']) != (selected['batch_id'], selected['variant_key']):
            continue
        if key in chosen:
            continue
        chosen.add(key)
        account = accounts.setdefault(series, dict(
            account_id=account_id, method_identity=method, variant_key=row['variant_key'],
            label=row.get('label', ''), model=row.get('model'), rules=deepcopy(saved['rules']),
            equity=initial, sessions=0, curve=[], _pnls=[]))
        account.update(variant_key=row['variant_key'], label=row.get('label', ''), model=row.get('model'))
        pnl = row.get('net_pnl')
        complete = (row.get('status') in ('final', 'provisional', 'empty')
                    and isinstance(pnl, (int, float)) and not isinstance(pnl, bool)
                    and math.isfinite(pnl))
        if not complete:
            continue
        account['_pnls'].append(pnl)
        cumulative = math.fsum(account['_pnls'])
        balance = initial + cumulative
        row.update(balance_preview_counted=True, account_balance=balance,
                   account_cumulative_net_pnl=cumulative)
        account['equity'] = balance
        account['sessions'] += 1
        account['curve'].append(dict(date=row['trade_date'], equity=balance))
    for account in accounts.values():
        del account['_pnls']
    view.update(accounts=[accounts[k] for k in sorted(accounts)], local_balance_preview=True)
    return view
