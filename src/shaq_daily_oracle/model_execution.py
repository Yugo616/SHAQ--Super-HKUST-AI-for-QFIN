"""Runtime-only call policy and versioned, reversible evidence encoding.

These settings intentionally never mutate a historical ModelProfile identity.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

from .background_process import background_process_options


@dataclass(frozen=True)
class ExecutionPolicy:
    timeout_seconds: int = 600
    transient_retries: int = 1

    def __post_init__(self):
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError('call timeout must be a positive integer')
        if type(self.transient_retries) is not int or not 0 <= self.transient_retries <= 1:
            raise ValueError('transient retries must be zero or one')

    def public_dict(self):
        return asdict(self)


_POLICY: ContextVar[ExecutionPolicy] = ContextVar('model_execution_policy', default=ExecutionPolicy())


@contextmanager
def execution_policy_scope(policy: ExecutionPolicy):
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)


def call_timeout_seconds():
    return _POLICY.get().timeout_seconds


def transient_model_failure(error: Exception) -> bool:
    """Fail closed: transport failures only, never arbitrary provider text."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostic = getattr(current, 'diagnostic', None) or {}
        status = diagnostic.get('status', getattr(current, 'status_code', None))
        if isinstance(status, int):
            return status == 429 or 500 <= status <= 599
        if diagnostic.get('kind') in {'authentication', 'permission', 'schema'}:
            return False
        if diagnostic.get('kind') in {'timeout', 'connection'}:
            return True
        if isinstance(current, (TimeoutError, ConnectionError, subprocess.TimeoutExpired)):
            return True
        if type(current).__module__.split('.')[0] in {'httpx', 'httpcore', 'openai'} and type(current).__name__ in {
            'TimeoutException', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
            'ConnectError', 'ReadError', 'WriteError', 'NetworkError', 'RemoteProtocolError',
            'APITimeoutError', 'APIConnectionError',
        }:
            return True
        current = current.__cause__
    return False


def run_model_process(command, *, timeout, input=None, capture_output=False, check=False, **kwargs):
    """A dedicated process group owns exactly this call, never other app sessions."""
    options = {**background_process_options(), **kwargs}
    if sys.platform != 'win32':
        options['start_new_session'] = True
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        options['stdin'] = subprocess.PIPE
    with subprocess.Popen(command, **options) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            if sys.platform == 'win32':
                # Native executable only; /T targets this PID and descendants, /F avoids an orphaned call.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    capture_output=True, timeout=10, check=False, **background_process_options())
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill() if process.poll() is None else None
            process.communicate()
            raise
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed


def compact_market_tables(value: Any) -> Any:
    """Encode bars with a presence bitmap, distinguishing absent fields from JSON null."""
    if isinstance(value, list):
        return [compact_market_tables(row) for row in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'bars' and isinstance(item, list) and item and all(isinstance(row, dict) for row in item):
            columns = sorted({field for row in item for field in row})
            result[key] = {'$shaq_bars': 1, 'columns': columns,
                'rows': [[compact_market_tables(row.get(field)) for field in columns] for row in item],
                'present': [None if len(row) == len(columns) else
                    [index for index, field in enumerate(columns) if field in row] for row in item]}
        else:
            result[key] = compact_market_tables(item)
    return result


def expand_market_tables(value: Any) -> Any:
    if isinstance(value, list):
        return [expand_market_tables(row) for row in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {'$shaq_bars', 'columns', 'rows', 'present'} and value['$shaq_bars'] == 1:
        return [{value['columns'][index]: expand_market_tables(row[index])
                 for index in (range(len(value['columns'])) if present is None else present)}
                for row, present in zip(value['rows'], value['present'], strict=True)]
    return {key: expand_market_tables(item) for key, item in value.items()}
