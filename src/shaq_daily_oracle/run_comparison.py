"""Display-only comparisons of already verified frozen runs, never prediction inputs."""
from difflib import unified_diff

from .hashing import sha256_payload


_UNRESOLVED_RESPONSE_MODELS = frozenset({'subscription-default'})


def _difference(left, right):
    status = 'unknown' if left is None or right is None else 'same' if left == right else 'different'
    return {'status': status, 'left': left, 'right': right}


def _value(value):
    return value if isinstance(value, str) and value.strip() else None


def _resolved_response_model(audit):
    response_model = _value(audit.get('response_model'))
    if response_model is None or response_model.strip().casefold() in _UNRESOLVED_RESPONSE_MODELS:
        return None
    return response_model


def _side(batch, key):
    variant = batch.get('variants', {}).get(key)
    if not isinstance(variant, dict):
        raise ValueError('该运行中没有所选版本，请重新选择。')
    documents = batch.get('skill_snapshots', {}).get(key, {}).get('documents')
    evidence = batch.get('evidence', {})
    intake = variant.get('candidate_intake', {})
    candidates = intake.get('candidates') if 'candidates' in intake else evidence.get('candidates')
    account = next((row for row in batch.get('virtual_accounts', {}).get('results', [])
                    if row.get('variant_key') == key), {})
    rules = _value(account.get('execution_policy_hash'))
    # Current projection rules are not necessarily the historical execution rules.
    execution = {'policy_hash': rules, 'engine': account.get('engine'),
                 'engine_version': account.get('engine_version')} if rules else None
    profile = _value(variant.get('model_profile_sha256'))
    audits = variant.get('model_call_audits', [])
    policies = []
    response_models = []
    for audit in audits:
        policy = _value(audit.get('request_policy_sha256'))
        if not policy and isinstance(audit.get('request_policy'), dict):
            policy = sha256_payload(audit['request_policy'])
        policies.append(policy)
        response_models.append(_resolved_response_model(audit))
    model = {
        'profile': profile,
        'request_policies': sorted(set(policies)),
        'response_models': sorted(set(response_models)),
    } if (profile and policies and all(policies) and all(response_models)) else None
    return {
        'batch_id': batch.get('batch_id'), 'variant_key': key,
        'label': variant.get('variant', {}).get('label') or key,
        'variant': variant, 'documents': documents,
        'method': sha256_payload(documents) if isinstance(documents, dict) and documents else None,
        'model': model,
        'data': _value(evidence.get('evidence_hash')),
        'candidates': sha256_payload(candidates) if isinstance(candidates, list) else None,
        'trade_date': str(evidence['as_of_et'])[:10] if evidence.get('as_of_et') else None,
        'trading_rules': execution,
        'outcome': {'status': account.get('status', 'unavailable'),
                    'net_pnl': account.get('net_pnl'), 'fees': account.get('fees'),
                    'slippage_cost': account.get('slippage_cost'),
                    'balance': account.get('account_balance'), 'scope': account.get('scope')},
    }


def compare_runs(left_batch, left_key, right_batch, right_key):
    left, right = _side(left_batch, left_key), _side(right_batch, right_key)
    dimensions = {name: _difference(left[name], right[name]) for name in
                  ('method', 'model', 'data', 'candidates', 'trading_rules', 'trade_date')}
    changed = []
    if isinstance(left['documents'], dict) and isinstance(right['documents'], dict):
        for path in sorted(set(left['documents']) | set(right['documents'])):
            a, b = left['documents'].get(path, ''), right['documents'].get(path, '')
            if a != b:
                changed.append({'path': path, 'diff': ''.join(unified_diff(
                    a.splitlines(keepends=True), b.splitlines(keepends=True),
                    fromfile='left/' + path, tofile='right/' + path))})
    predictions = [{p['symbol']: p['direction'] for p in side['variant'].get('predictions', [])}
                   for side in (left, right)]
    symbols = sorted(set(predictions[0]) | set(predictions[1]) |
                     set(left['variant'].get('reports_by_symbol', {})) |
                     set(right['variant'].get('reports_by_symbol', {})))
    controlled = dimensions['method']['status'] != 'unknown' and all(
        dimensions[name]['status'] == 'same' for name in dimensions if name != 'method')
    return {
        'left': {k: left[k] for k in ('batch_id', 'variant_key', 'label', 'trade_date')},
        'right': {k: right[k] for k in ('batch_id', 'variant_key', 'label', 'trade_date')},
        'dimensions': dimensions, 'controlled_method_comparison': controlled,
        'changed_files': changed,
        'stocks': [{'symbol': symbol, 'left': predictions[0].get(symbol, 'not_published'),
                    'right': predictions[1].get(symbol, 'not_published'),
                    'left_reason': left['variant'].get('integration_audit', {}).get(symbol, {}),
                    'right_reason': right['variant'].get('integration_audit', {}).get(symbol, {})}
                   for symbol in symbols],
        'outcomes': {'left': left['outcome'], 'right': right['outcome']},
    }
