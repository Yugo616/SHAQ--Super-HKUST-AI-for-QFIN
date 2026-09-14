"""Single-operation Yahoo worker: no GUI, credentials, or parent-local Yahoo caches."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import date

from .model_execution import run_model_process
from .model_http_worker import _worker_stream


def _failure_diagnostic(exc, stage):
    cause = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    code = getattr(cause, 'errno', None)
    return {'kind': 'resource_exhausted' if code in {errno.EMFILE, errno.ENFILE} else 'provider_error',
            'error_type': type(cause).__name__, 'errno': code if isinstance(code, int) else None,
            'stage': stage}


def call_in_worker(operation, profile, payload):
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
        'maximum_option_contracts_per_side', 'intraday_interval'}}
    try:
        # Parent owns scratch cleanup even after a forced owned-tree termination.
        with tempfile.TemporaryDirectory(prefix='shaq-collection-') as cache_parent:
            completed = run_model_process(command, input=json.dumps({
                'operation': operation, 'payload': {**payload, 'profile': source,
                                                   'cache_parent': cache_parent}}),
                text=True, encoding='utf-8', errors='replace', capture_output=True,
                timeout=profile.yahoo_worker_timeout_seconds, env=environment, shell=False, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DataProviderError('Yahoo collection worker deadline exceeded',
                                diagnostic={'kind':'timeout','stage':operation}) from exc
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
        diagnostic = envelope.get('diagnostic') or {}
        # Only the structured allowlist crosses into a durable parent job record.
        diagnostic = {key: value for key, value in diagnostic.items() if key in {
            'kind', 'error_type', 'errno', 'stage'}} if isinstance(diagnostic, dict) else {}
        message = ('Yahoo collection worker resource exhausted' if diagnostic.get('kind') == 'resource_exhausted'
                   else 'Yahoo collection provider request failed')
        raise DataProviderError(message, diagnostic=diagnostic)
    if not isinstance(envelope, dict) or 'error' in envelope or not isinstance(envelope.get('result'), dict):
        # Never expose third-party stderr, exception text, headers or payloads.
        raise DataProviderError('Yahoo collection worker could not complete request')
    return envelope['result']


def execute_operation(operation, payload):
    if operation == 'ping':
        return {'worker': 'yahoo-collection', 'protocol_version': 1}
    if operation not in {'history', 'option_surface'}:
        raise ValueError('unsupported collection operation')
    from .data_providers import DataProfile, YFinanceProvider
    from curl_cffi.requests import Session
    profile = DataProfile.from_dict(payload['profile'])
    provider = YFinanceProvider(profile)
    yf = provider._module()
    # This directory is parent-independent and never contains a durable request.
    # ExitStack closes the session/databases before deleting it (also on Windows).
    with tempfile.TemporaryDirectory(prefix='shaq-yahoo-', dir=payload.get('cache_parent')) as cache_root:
        yf.set_tz_cache_location(cache_root)
        with contextlib.ExitStack() as cleanup:
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
        if request.get('operation') in {'history', 'option_surface', 'ping'}:
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
