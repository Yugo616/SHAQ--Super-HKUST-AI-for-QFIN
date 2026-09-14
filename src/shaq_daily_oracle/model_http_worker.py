"""Credential-in-memory HTTP transport worker with an actual parent-owned deadline."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from .model_execution import run_model_process, transient_model_failure


def execute_transport(operation, payload):
    from .model_backends import ModelProfile, _http_post_json_inline, _openai_responses_call_inline
    if operation == 'http-json':
        return _http_post_json_inline(**payload)
    if operation == 'openai-responses':
        return _openai_responses_call_inline(**{**payload, 'profile': ModelProfile.from_dict(payload['profile'])})
    raise ValueError('unsupported isolated transport operation')


def call_in_worker(operation, payload, *, timeout):
    from .model_backends import ModelBackendError, _local_cli_environment
    command = ([sys.executable, '--model-http-worker'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-m', 'shaq_daily_oracle.model_http_worker'])
    environment = _local_cli_environment()
    # Preserve only the transport's explicit proxy/CA/import configuration, not arbitrary secrets.
    for key, value in os.environ.items():
        if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                           'SSL_CERT_FILE', 'SSL_CERT_DIR', 'PYTHONPATH'}:
            environment[key] = value
    try:
        completed = run_model_process(command, input=json.dumps({'operation': operation, 'payload': payload}),
            text=True, encoding='utf-8', errors='replace', capture_output=True,
            timeout=timeout, env=environment, shell=False, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ModelBackendError('model endpoint request failed: total call deadline exceeded',
                                diagnostic={'kind': 'timeout'}) from exc
    if completed.returncode != 0:
        # Worker stderr is never persisted or displayed: third-party exceptions may contain headers.
        raise ModelBackendError('model transport worker failed', diagnostic={'kind': 'worker_failure'})
    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise ModelBackendError('model transport worker returned malformed output') from exc
    if not isinstance(envelope, dict):
        raise ModelBackendError('model transport worker returned invalid envelope')
    if 'error' in envelope:
        raise ModelBackendError(str(envelope['error']), diagnostic=envelope.get('diagnostic'))
    if 'result' not in envelope:
        raise ModelBackendError('model transport worker returned no result')
    return envelope['result']


def _worker_stream(name, handle_id, mode):
    stream = getattr(sys, name)
    if stream is not None:
        return stream
    if sys.platform == 'win32':
        # PyInstaller --windowed sets Python std streams to None; STARTUPINFO still
        # supplies inherited pipe handles. Attach those handles, never allocate a console.
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(handle_id)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError('model worker inherited pipe is unavailable')
        descriptor = msvcrt.open_osfhandle(int(handle),
            os.O_BINARY | (os.O_RDONLY if mode == 'r' else os.O_WRONLY))
    else:
        descriptor = os.dup(0 if mode == 'r' else 1)
    return os.fdopen(descriptor, mode, encoding='utf-8')


def main():
    from .model_backends import ModelBackendError, safe_model_error_summary
    incoming = _worker_stream('stdin', -10, 'r')
    outgoing = _worker_stream('stdout', -11, 'w')
    try:
        request = json.load(incoming)
        result = execute_transport(request['operation'], request['payload'])
        envelope = {'result': result}
    except Exception as exc:
        # Backend errors already redact auth headers and secret values before crossing this pipe.
        if isinstance(exc, ModelBackendError):
            diagnostic = exc.diagnostic
            if not diagnostic and transient_model_failure(exc):
                diagnostic = {'kind': 'connection'}
            envelope = {'error': safe_model_error_summary(exc), 'diagnostic': diagnostic}
        else:
            envelope = {'error': 'model transport worker could not complete request',
                        'diagnostic': {'kind': 'worker_failure'}}
    outgoing.write(json.dumps(envelope, ensure_ascii=False))
    outgoing.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
