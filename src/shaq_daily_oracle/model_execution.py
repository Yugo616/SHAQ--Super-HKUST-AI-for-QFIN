"""Runtime-only call policy and versioned, reversible evidence encoding.

These settings intentionally never mutate a historical ModelProfile identity.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

from .background_process import background_process_options
from .windows_process_tree import WindowsProcessJob


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


QUOTA_ERROR_CODES = frozenset({'insufficient_quota', 'usage_limit_reached', 'quota_exceeded'})


def transient_model_failure(error: Exception) -> bool:
    """Fail closed: transport failures only, never arbitrary provider text."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostic = getattr(current, 'diagnostic', None) or {}
        if diagnostic.get('kind') == 'quota' or diagnostic.get('provider_code') in QUOTA_ERROR_CODES:
            return False
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
    owner = WindowsProcessJob() if sys.platform == 'win32' else None
    if owner is not None:
        options['creationflags'] = options.get('creationflags', 0) | 0x08000004  # hidden + suspended until assigned
    else:
        options['start_new_session'] = True
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        options['stdin'] = subprocess.PIPE
    process = None
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, **options)
        try:
            if owner is not None:
                owner.assign_and_resume(process)
            stdout, stderr = process.communicate(input=input, timeout=max(0, timeout - (time.monotonic() - started)))
        except BaseException as original:
            try:
                if owner is not None:
                    owner.terminate(timeout=1)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as cleanup_error:
                original.add_note(f'Owned tree cleanup: {type(cleanup_error).__name__}')
            finally:
                if owner is not None:
                    try:
                        owner.close()  # Independently enforces kill-on-close if explicit termination failed.
                    except Exception as cleanup_error:
                        original.add_note(f'Owned job closure: {type(cleanup_error).__name__}')
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    original.add_note(f'Owned parent cleanup: {type(cleanup_error).__name__}')
            # Never drain inherited pipes after a timeout or enter Popen.__exit__'s unbounded wait.
            raise
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed
    finally:
        if owner is not None and owner.handle:
            pending_error = sys.exc_info()[1]
            try:
                owner.close()
            except Exception as cleanup_error:
                if pending_error is not None:
                    pending_error.add_note(f'Owned job final closure: {type(cleanup_error).__name__}')
                else:
                    raise
        if process is not None:
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(process, name, None)
                reader = getattr(process, f'{name}_thread', None)
                # A Windows communicate reader can hold a buffered-stream lock. It is daemonized;
                # never block on close if OS cleanup failed and that reader is still active.
                if stream is not None and not (reader is not None and reader.is_alive()):
                    stream.close()


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
