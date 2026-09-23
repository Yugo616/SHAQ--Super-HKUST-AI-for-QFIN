"""Single-operation Yahoo worker: no GUI, credentials, or parent-local Yahoo caches."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .model_execution import run_model_process
from .model_http_worker import _worker_stream
from .data_retry import failure_diagnostic, failure_message, sanitize_diagnostic
from .research_progress import safe_observe


def _failure_diagnostic(exc, stage):
    return failure_diagnostic(exc, stage)


def _forward_progress(path, observer, stop):
    with path.open('r', encoding='utf-8') as stream:
        while True:
            position = stream.tell()
            line = stream.readline()
            if line and line.endswith('\n'):
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict) and isinstance(event.get('stage'), str):
                    safe_observe(observer, **event)
                continue
            if line:
                stream.seek(position)  # Wait for the writer's complete JSONL record.
            if stop.is_set():
                return
            stop.wait(0.02)


def call_in_worker(operation, profile, payload, *, progress_observer=None):
    from .data_providers import DataProviderError
    from .model_backends import _local_cli_environment
    command = ([sys.executable, '--collection-worker'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-m', 'shaq_daily_oracle.collection_worker'])
    environment = _local_cli_environment()
    for key, value in os.environ.items():
        if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                           'SSL_CERT_FILE', 'SSL_CERT_DIR', 'PYTHONPATH'}:
            environment[key] = value
    # Explicit allowlist: no SEC identity, endpoint credentials or whole settings.
    source = {key: value for key, value in asdict(profile).items() if key in {
        'profile_id', 'universe_file', 'request_timeout_seconds', 'batch_size',
        'maximum_option_expiries',
        'maximum_option_contracts_per_side', 'intraday_interval',
        'yahoo_request_max_retries', 'yahoo_retry_backoff_seconds'}}
    try:
        # Parent owns scratch cleanup even after a forced owned-tree termination.
        with tempfile.TemporaryDirectory(prefix='shaq-collection-') as cache_parent:
            progress_path = Path(cache_parent) / 'progress.jsonl' if progress_observer is not None else None
            stop = thread = None
            if progress_path is not None:
                try:
                    progress_path.touch()
                    stop = threading.Event()
                    thread = threading.Thread(target=_forward_progress,
                                              args=(progress_path, progress_observer, stop),
                                              daemon=True, name='shaq-collection-progress')
                    thread.start()
                except (OSError, RuntimeError):
                    # A display channel failure cannot cancel market data.
                    if stop is not None:
                        stop.set()
                    if thread is not None and thread.is_alive():
                        thread.join(timeout=1)
                    progress_path = stop = thread = None
            try:
                child_payload = {**payload, 'profile': source, 'cache_parent': cache_parent}
                if progress_path is not None:
                    child_payload['progress_path'] = str(progress_path)
                completed = run_model_process(command, input=json.dumps({
                    'operation': operation, 'payload': child_payload}),
                    text=True, encoding='utf-8', errors='replace', capture_output=True,
                    timeout=profile.yahoo_worker_timeout_seconds, env=environment, shell=False, check=False)
            finally:
                if stop is not None:
                    stop.set()
                    thread.join(timeout=1)
    except subprocess.TimeoutExpired as exc:
        # A parent deadline identifies the requested group, not the ticker
        # active when the worker stopped; never invent a per-ticker cause.
        diagnostic = sanitize_diagnostic({'kind': 'timeout', 'stage': operation,
            'timeout_scope': 'worker', 'requested_symbols': payload.get('symbols'),
            'symbol': payload.get('symbol'), 'request_start': payload.get('start'),
            'request_end': payload.get('end'), 'interval': payload.get('interval')})
        raise DataProviderError('Yahoo collection worker deadline exceeded',
                                diagnostic=diagnostic) from exc
    except OSError as exc:
        raise DataProviderError('Yahoo collection worker could not start',
                                diagnostic=_failure_diagnostic(exc, 'startup')) from exc
    if completed.returncode != 0:
        raise DataProviderError('Yahoo collection worker crashed',
                                diagnostic={'kind':'worker_crash','stage':operation,
                                            'returncode':completed.returncode})
    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise DataProviderError('Yahoo collection worker returned malformed output',
                                diagnostic={'kind':'protocol_error','stage':operation}) from exc
    if isinstance(envelope, dict) and 'error' in envelope:
        diagnostic = sanitize_diagnostic(envelope.get('diagnostic'), operation)
        raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic)
    if not isinstance(envelope, dict) or 'error' in envelope or not isinstance(envelope.get('result'), dict):
        # Never expose third-party stderr, exception text, headers or payloads.
        raise DataProviderError('Yahoo collection worker could not complete request')
    return envelope['result']


def execute_operation(operation, payload):
    if operation == 'ping':
        return {'worker': 'yahoo-collection', 'protocol_version': 1}
    if operation not in {'history', 'option_surface', 'public_history'}:
        raise ValueError('unsupported collection operation')
    from .data_providers import DataProfile, YFinanceProvider
    from curl_cffi.requests import Session
    profile = DataProfile.from_dict(payload['profile'])
    progress_path = payload.get('progress_path')
    progress_observer = None
    if progress_path is not None:
        progress_file = Path(progress_path).resolve()
        cache_parent = Path(payload['cache_parent']).resolve()
        if not progress_file.is_relative_to(cache_parent):
            raise ValueError('invalid collection progress path')

        def progress_observer(**event):
            with progress_file.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + '\n')
                stream.flush()

    provider = YFinanceProvider(profile, progress_observer=progress_observer)
    provider.history_checkpoint_root = payload.get('history_checkpoint_root')
    provider.history_source_identity = payload.get('history_source_identity', profile.history_identity())
    if operation == 'public_history':
        from .public_history_recovery import PublicHistoryRecovery
        recovery = PublicHistoryRecovery(provider, payload['recovery_config'],
            checkpoint_root=payload.get('history_checkpoint_root'), etfs=payload.get('etfs', []))
        rows = recovery._history_inline(payload['symbols'],
            start=date.fromisoformat(payload['start']), end=date.fromisoformat(payload['end']),
            interval=payload.get('interval', '1d'), prepost=payload.get('prepost', False))
        return {'rows': rows, 'diagnostics': recovery.diagnostics,
                'source_documents': recovery.source_documents}
    yf = provider._module()
    # This directory is parent-independent and never contains a durable request.
    # ExitStack closes the session/databases before deleting it (also on Windows).
    with tempfile.TemporaryDirectory(prefix='shaq-yahoo-', dir=payload.get('cache_parent')) as cache_root:
        yf.set_tz_cache_location(cache_root)
        with contextlib.ExitStack() as cleanup:
            # Public Yahoo configuration, confined to this owned single-thread
            # process; never replace dependency functions or swallow HTTP errors.
            hidden_errors = yf.config.debug.hide_exceptions
            yf.config.debug.hide_exceptions = False
            cleanup.callback(setattr, yf.config.debug, 'hide_exceptions', hidden_errors)
            for getter in (yf.cache.get_tz_cache, yf.cache.get_cookie_cache, yf.cache.get_isin_cache):
                cache = getter()
                def close_cache(cache=cache):
                    if getattr(cache, 'db', None) is not None:
                        cache.db.close()
                cleanup.callback(close_cache)
            session = cleanup.enter_context(Session(impersonate='chrome'))
            cleanup.callback(yf.data.YfData.cache_get.cache_clear)
            if operation == 'history':
                return provider._history_inline(payload['symbols'],
                    start=date.fromisoformat(payload['start']), end=date.fromisoformat(payload['end']),
                    interval=payload.get('interval', '1d'), prepost=payload.get('prepost', False),
                    session=session)
            return provider._option_surface_inline(payload['symbol'], session=session)


def main():
    incoming = _worker_stream('stdin', -10, 'r')
    outgoing = _worker_stream('stdout', -11, 'w')
    stage = 'startup'
    try:
        request = json.load(incoming)
        if request.get('operation') in {'history', 'option_surface', 'public_history', 'ping'}:
            stage = request['operation']
        # Provider prints must not corrupt the result channel; stderr is discarded
        # by the parent and never written into evidence or job records.
        with open(os.devnull, 'w', encoding='utf-8') as quiet, \
                contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = execute_operation(request['operation'], request['payload'])
        envelope = {'result': result}
    except Exception as exc:
        envelope = {'error': 'collection_failed', 'diagnostic': _failure_diagnostic(exc, stage)}
    outgoing.write(json.dumps(envelope, ensure_ascii=False))
    outgoing.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
