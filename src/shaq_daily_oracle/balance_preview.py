"""Optional local display projection; never writes settlements or score eligibility."""
from copy import deepcopy
from datetime import datetime, timezone
import math


def project_saved_balances(saved, daily_rows):
    """Sum saved daily PnL once per method, including late research records.

    Not an account replay: quantities, fills and costs remain unchanged. The
    first saved run by actual completion wins, never the most profitable run.
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

    accounts, chosen = {}, set()
    for row in sorted(rows, key=order):
        row.update(balance_preview_counted=False, account_balance=None,
                   account_cumulative_net_pnl=None)
        if row.get('status') == 'duplicate':
            continue
        info = metadata.get((row['batch_id'], row['variant_key']), {})
        method = (row.get('method_identity') or info.get('method_identity')
                  or row.get('series_key') or info.get('series_key') or row['variant_key'])
        account_id = 'local-preview:' + method
        row['account_id'] = account_id
        key = method, row['trade_date']
        if key in chosen:
            continue
        chosen.add(key)
        account = accounts.setdefault(method, dict(
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
