"""Discover model choices without sending an inference request or a user packet."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .background_process import background_process_options
from .model_backends import (ModelBackendError, ModelProfile, _cli_command, _local_cli,
                             _local_cli_environment, _endpoint, _auth_headers)
from .windows_process_tree import WindowsProcessJob


def normalize_catalog(protocol, rows):
    choices, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('hidden'):
            continue
        key = 'value' if protocol == 'claude-code' else 'model'
        model = str(row.get(key) or row.get('id') or '').strip()
        if not model or model in {'default', 'subscription-default'} or model in seen:
            continue
        seen.add(model)
        choices.append({'id': model, 'label': str(row.get('displayName') or row.get('display_name') or model)})
    return choices


def _exchange(command, protocol, timeout=30):
    """Metadata-only stdio handshake. Own and close exactly this subprocess tree."""
    options = background_process_options()
    owner = WindowsProcessJob() if sys.platform == 'win32' else None
    if owner:
        options['creationflags'] = options.get('creationflags', 0) | 0x08000004
    else:
        options['start_new_session'] = True
    process = None
    messages = queue.Queue()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix='shaq-model-list-') as root:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                shell=False, cwd=root, env=_local_cli_environment(), **options)
            if owner:
                owner.assign_and_resume(process)

            def read():
                try:
                    for line in process.stdout:
                        if len(line) > 2_000_000:
                            break
                        messages.put(line)
                finally:
                    messages.put(None)
            reader = threading.Thread(target=read, daemon=True)
            reader.start()

            def send(value):
                process.stdin.write(json.dumps(value) + '\n')
                process.stdin.flush()

            def receive(match):
                while True:
                    try:
                        line = messages.get(timeout=max(0.001, deadline - time.monotonic()))
                    except queue.Empty as exc:
                        raise ModelBackendError('读取模型列表超时，请重试；未发起分析调用。') from exc
                    if line is None or time.monotonic() > deadline:
                        raise ModelBackendError('本机工具未返回模型列表，请更新工具或手动填写型号。')
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and match(value):
                        return value

            if protocol == 'codex-cli':
                send({'id': 1, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'shaq_model_picker', 'version': '1'}}})
                if receive(lambda v: v.get('id') == 1).get('error'):
                    raise ModelBackendError('Codex 模型列表初始化失败，请更新 Codex。')
                send({'method': 'initialized', 'params': {}})
                rows, cursor, cursors = [], None, set()
                while True:
                    send({'id': 2, 'method': 'model/list', 'params': {
                        'limit': 100, 'includeHidden': False, 'cursor': cursor}})
                    result = receive(lambda v: v.get('id') == 2).get('result')
                    if not isinstance(result, dict):
                        raise ModelBackendError('Codex 无法获取可用模型，请检查登录。')
                    rows.extend(result.get('data', []))
                    cursor = result.get('nextCursor')
                    if not cursor:
                        return rows
                    if cursor in cursors:
                        raise ModelBackendError('模型列表分页无效')
                    cursors.add(cursor)
            send({'type': 'control_request', 'request_id': 'shaq-models',
                  'request': {'subtype': 'initialize', 'hooks': {}, 'agents': {}, 'skills': []}})
            envelope = receive(lambda v: v.get('type') == 'control_response' and
                               v.get('response', {}).get('request_id') == 'shaq-models')
            result = envelope.get('response', {}).get('response', {})
            return result.get('models', [])
        finally:
            if process:
                try:
                    if owner:
                        owner.close()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                if 'reader' in locals():
                    reader.join(timeout=1)
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
            elif owner:
                owner.close()


def local_model_catalog(protocol):
    if protocol not in {'codex-cli', 'claude-code'}:
        raise ModelBackendError('请选择 Codex 或 Claude Code 连接')
    profile = ModelProfile('model-list', protocol, '', 'catalog-only')
    executable = _local_cli(profile)
    # Match the research backend's default provider; never start a thread/turn.
    arguments = (['app-server', '-c', 'model_provider="openai"'] if protocol == 'codex-cli' else
                 ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                  '--verbose', '--permission-mode', 'plan', '--tools', '', '--strict-mcp-config'])
    models = normalize_catalog(protocol, _exchange(_cli_command(executable, arguments), protocol))
    if not models:
        raise ModelBackendError('该工具未提供可选模型列表；请手动填写明确型号并测试，不能使用默认型号。')
    return {'protocol': protocol, 'models': models}


def api_model_catalog(profile_value, secret):
    import httpx
    profile = ModelProfile.from_dict(profile_value)
    if profile.protocol in {'codex-cli', 'claude-code'}:
        raise ModelBackendError('请选择 API 连接')
    headers = _auth_headers(profile, secret)
    if profile.protocol == 'anthropic-messages':
        headers['anthropic-version'] = '2023-06-01'
    try:
        response = httpx.get(_endpoint(profile.base_url, '/v1/models'), headers=headers,
                             timeout=20, follow_redirects=False)
        response.raise_for_status()
        value = response.json()
    except httpx.HTTPStatusError as exc:
        raise ModelBackendError(f'读取模型列表失败（HTTP {exc.response.status_code}）；可手动填写服务商提供的型号。') from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelBackendError('无法读取模型列表；请检查连接，或手动填写服务商提供的型号。') from exc
    models = normalize_catalog(profile.protocol, value.get('data', []) if isinstance(value, dict) else [])
    if not models:
        raise ModelBackendError('接口未提供模型列表，请手动填写明确型号。')
    return {'protocol': profile.protocol, 'models': models}
