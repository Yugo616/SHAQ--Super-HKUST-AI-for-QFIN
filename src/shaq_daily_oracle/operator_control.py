"""Desktop control of the existing local operator; never a second trading engine."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .market_calendar import market_session
from .settings import _atomic_json


def _read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else (default or {})


def requested_today(paths, day):
    return _read(paths.data_root / 'operator-request.json').get('session_date') == day.isoformat()


def _wake(label):
    if sys.platform != 'darwin':
        raise ValueError('正式模拟盘只连接本机现有Mac操作员服务')
    result = subprocess.run(['launchctl', 'kickstart', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or '正式后台尚未安装或无法启动')


def request_today(paths, *, now=None, wake=None):
    config = _read(paths.config_root / 'operator-service.json')
    label = config.get('launch_agent_label', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]+', label):
        raise ValueError('本机尚未连接原正式模拟盘服务；研究版本不会代替正式下单')
    now = now or datetime.now(ZoneInfo('America/New_York'))
    if market_session(now.date()) is None:
        raise ValueError('今日美股休市，不启动正式模拟盘')
    already = requested_today(paths, now.date())
    if not already:
        _atomic_json(paths.data_root / 'operator-request.json', {
            'session_date': now.date().isoformat(), 'requested_at_et': now.isoformat(),
            'requested_mode': 'paper', 'source': 'desktop',
        })
    (wake or _wake)(label)
    return {'session_date': now.date().isoformat(), 'already_requested': already,
            'status': 'requested', 'message': '已交给原正式后台；采集和交易仍遵守原时间表'}


def status(paths, *, now=None):
    now = now or datetime.now(ZoneInfo('America/New_York'))
    runs = []
    for folder in sorted(paths.runtime_root.glob(f'SHAQ-CANARY-{now.date().isoformat()}-*')):
        stages = _read(folder / 'workflow_state.json').get('stages', {})
        latest = max(stages, key=lambda key: stages[key].get('observed_at_et', ''), default='')
        phase = stages.get(latest, {})
        runs.append({'run_id': folder.name, 'stage': latest,
                     'status': phase.get('status', 'starting'), 'detail': phase.get('detail', ''),
                     'has_frozen_result': (folder / 'frozen_run.json').is_file(),
                     'complete': (folder / 'audit_complete.json').is_file()})
    return {'connected': (paths.config_root / 'operator-service.json').is_file(),
            'session_date': now.date().isoformat(), 'requested': requested_today(paths, now.date()),
            'service': _read(paths.runtime_root / 'service_status.json'), 'runs': runs}
