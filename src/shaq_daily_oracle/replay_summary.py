"""Read-only outcome summaries: association is not a causal explanation."""
from decimal import Decimal


def candidate_summary(variant, symbol, label):
    prediction = next((p for p in variant.get('predictions', []) if p['symbol'] == symbol), None)
    decision = next((d for d in variant.get('synthesis', {}).get('decisions', []) if d['symbol'] == symbol), {})
    reports = variant.get('reports_by_symbol', {}).get(symbol, [])
    basis = ([{'domain': 'synthesis', 'thesis': decision.get('thesis', ''),
               'antithesis': decision.get('antithesis', ''), 'evidence_ids': decision.get('evidence_ids', [])}]
             if decision else [{k: r.get(k, [] if k == 'evidence_ids' else '')
                                for k in ('domain', 'thesis', 'antithesis', 'evidence_ids')}
                               for r in reports if prediction and r.get('verdict') == prediction['direction']])
    result = {'symbol': symbol, 'basis': basis, 'correct': None, 'return_pct': None,
              'source_result_sha256': variant.get('variant_result_sha256'),
              'explanation': '等待最终价格核验；现在不解释预测对错。'}
    if label.get('status') != 'final':
        return result
    opening = Decimal(str(label['official_unadjusted_open']))
    close = Decimal(str(label['official_unadjusted_close']))
    if not opening.is_finite() or not close.is_finite() or opening <= 0 or close <= 0:
        raise ValueError('invalid final label price')
    result['return_pct'] = float((close - opening) / opening * 100)
    actual = 'bullish' if close > opening else 'bearish' if close < opening else 'neutral'
    if not prediction:
        result['explanation'] = '该股票没有发布预测，不能计为预测正确或错误。价格变化不能反过来证明当时应当选它。'
    else:
        result['correct'] = prediction['direction'] == actual
        result['explanation'] = ('实际开盘至收盘方向与预测一致。' if result['correct'] else
                                 '实际开盘至收盘方向未支持原预测（平盘也计错）。') + \
            '以下是当时依据，不是事后原因证明；仅凭开收盘价格不能确定是哪条机制导致结果，也不能判断盘中何时反转。'
    return result


def compare_versions(left, right, left_documents, right_documents):
    changed = sorted(p for p in set(left_documents) | set(right_documents)
                     if left_documents.get(p) != right_documents.get(p))
    modules = sorted({p.split('/')[1] if p.startswith('skills/') else p.split('/')[0] for p in changed})
    a = {p['symbol']: p['direction'] for p in left.get('predictions', [])}
    b = {p['symbol']: p['direction'] for p in right.get('predictions', [])}
    symbols = sorted(set(a) | set(b) | set(left.get('reports_by_symbol', {})) | set(right.get('reports_by_symbol', {})))
    return {'same_model': left.get('model_profile_sha256') == right.get('model_profile_sha256'),
            'changed_modules': modules, 'changed_files': changed,
            'stocks': [{'symbol': s, 'left': a.get(s, 'not_published'), 'right': b.get(s, 'not_published')}
                       for s in symbols]}
