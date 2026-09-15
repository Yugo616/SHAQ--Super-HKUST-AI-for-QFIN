"""Safe Yahoo transport classification; never inspect or persist exception text."""
from __future__ import annotations

import errno
import re
import subprocess


_TRANSIENT = {'timeout', 'connection_error', 'rate_limited', 'provider_unavailable'}
_KINDS = _TRANSIENT | {'resource_exhausted', 'auth_error', 'no_data', 'provider_error',
                       'worker_crash', 'protocol_error'}
_STAGES = {'startup', 'history', 'option_surface', 'ping'}


def is_transient_diagnostic(diagnostic):
    return isinstance(diagnostic, dict) and diagnostic.get('kind') in _TRANSIENT


def sanitize_diagnostic(value, stage=None):
    """Only bounded numeric codes and fixed classifications cross the worker boundary."""
    value = value if isinstance(value, dict) else {}
    kind = value.get('kind')
    result = {'kind': kind if isinstance(kind, str) and kind in _KINDS else 'provider_error'}
    result['retryable'] = is_transient_diagnostic(result)
    selected_stage = stage if stage is not None else value.get('stage')
    if isinstance(selected_stage, str) and selected_stage in _STAGES:
        result['stage'] = selected_stage
    name = value.get('error_type')
    if isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', name):
        result['error_type'] = name
    for key, low, high in [('errno', 0, 4096), ('curl_code', 0, 999),
                           ('http_status', 100, 599), ('attempts', 1, 4),
                           ('retry_count', 0, 3), ('returncode', -255, 255)]:
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and low <= number <= high:
            result[key] = int(number)
    return result


def failure_diagnostic(exc, stage):
    seen = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        existing = getattr(exc, 'diagnostic', None)
        if isinstance(existing, dict) and existing.get('kind') in _KINDS:
            return sanitize_diagnostic(existing, stage)
        cause = exc.__cause__
        if cause is None or id(cause) in seen:
            break
        exc = cause
    name = type(exc).__name__
    number = getattr(exc, 'errno', None)
    curl_code = (getattr(exc, 'code', None)
                 if type(exc).__module__.startswith('curl_cffi.') else None)
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if status is None:
        status = getattr(exc, 'status_code', None)
    # urllib HTTPError uses .code for its HTTP status, not for a curl result.
    if status is None and type(exc).__module__ == 'urllib.error':
        status = getattr(exc, 'code', None)
    codes = sanitize_diagnostic({'errno': number, 'curl_code': curl_code, 'http_status': status})
    number, curl_code, status = (codes.get(key) for key in ('errno', 'curl_code', 'http_status'))
    kind = 'provider_error'
    if number in {errno.EMFILE, errno.ENFILE}:
        kind = 'resource_exhausted'
    elif status in {401, 403}:
        kind = 'auth_error'
    elif status == 429 or name == 'YFRateLimitError':
        kind = 'rate_limited'
    elif status is not None and status >= 500:
        kind = 'provider_unavailable'
    elif status is not None and status >= 400:
        kind = 'provider_error'
    elif (isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or curl_code == 28
          or number == errno.ETIMEDOUT):
        kind = 'timeout'
    elif (isinstance(exc, ConnectionError) or curl_code in {5, 6, 7, 18, 52, 55, 56, 92}
          or number in {errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
                        errno.ENETUNREACH, errno.EHOSTUNREACH}):
        kind = 'connection_error'
    elif name in {'YFPricesMissingError', 'YFTzMissingError'}:
        kind = 'no_data'
    return sanitize_diagnostic({**codes, 'kind': kind, 'error_type': name}, stage)


def failure_message(diagnostic):
    messages = {
        'timeout': 'Yahoo 数据请求超时',
        'connection_error': 'Yahoo 数据连接暂时中断',
        'rate_limited': 'Yahoo 数据请求暂时被限流',
        'provider_unavailable': 'Yahoo 数据服务暂时不可用',
        'auth_error': 'Yahoo 数据访问被拒绝，请检查网络或访问权限',
        'resource_exhausted': 'Yahoo collection worker resource exhausted（本地资源不足）',
        'no_data': 'Yahoo 未提供该时段的数据',
        'protocol_error': 'Yahoo 数据进程返回格式异常',
        'worker_crash': 'Yahoo 数据进程异常退出',
    }
    return messages.get(diagnostic.get('kind'), 'Yahoo 数据请求失败，未生成替代数据')
