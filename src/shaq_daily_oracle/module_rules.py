"""Small deterministic module hooks over frozen inputs."""
import json
from .decision_sandbox import _quickjs_context, _reject_forbidden_keys, MAX_OUTPUT_BYTES

MODULES = ('screening', 'market', 'relationships', 'event', 'capital', 'derivatives', 'price_volume')


def default_rule(module):
    if module == 'screening':
        return 'function compute(input) { return {symbols: input.candidates.slice(0, input.maximum_candidates).map(x => x.symbol)}; }'
    return 'function compute(input) { return {}; }'


def execute_rule(script, value):
    _reject_forbidden_keys(value)
    if len(script.encode()) > 100000:
        raise ValueError('模块代码过长')
    context = _quickjs_context()
    context.set('__input', json.dumps(value, ensure_ascii=False, allow_nan=False))
    raw = context.eval('"use strict";\n' + script + '\nJSON.stringify(compute(JSON.parse(__input)))')
    if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT_BYTES:
        raise ValueError('模块输出过大或不是JSON对象')
    result = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(result, dict):
        raise ValueError('模块必须输出对象')
    _reject_forbidden_keys(result)
    return result


def test_rule(script, cases):
    value = json.loads(cases)
    rows = value.get('cases', [])
    if not rows or not str(value.get('reference', '')).strip():
        raise ValueError('请提供研究依据和至少一项固定测试')
    for row in rows:
        if execute_rule(script, row['input']) != row['expected']:
            raise ValueError('固定测试失败：' + str(row.get('name', '未命名')))
    return {'status': 'passed', 'case_count': len(rows)}


def select_symbols(script, candidates, maximum):
    result = execute_rule(script, {'candidates': candidates, 'maximum_candidates': maximum})
    symbols = result.get('symbols')
    available = {c['symbol'] for c in candidates}
    if not isinstance(symbols, list) or any(not isinstance(x, str) for x in symbols):
        raise ValueError('筛选结果必须包含symbols列表')
    if len(symbols) != len(set(symbols)) or not set(symbols) <= available:
        raise ValueError('筛选结果包含重复或股票池之外的股票')
    return symbols
