"""Local research scheduler. This module never imports a broker."""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from .market_calendar import market_session, next_market_session
from .settings import _atomic_json
from .update_admission import guarded_worker
from .background_process import background_process_options
from .data_retry import is_transient_diagnostic
from .hashing import sha256_payload

ET = ZoneInfo("America/New_York")
SERVICE_LABEL = "org.shaq.daily-oracle.research"


def schedule_status(paths):
    path = paths.research_root / "schedule.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "enabled": False, "start_et": "08:35:00", "selections": [], "model_profile_id": "",
    }
    now = datetime.now(ET)
    session = market_session(now.date())
    if session is None:
        session = next_market_session(now.date())
    target = datetime.combine(session.session_date, clock_time.fromisoformat(value["start_et"]), ET)
    if target <= now:
        session = next_market_session(session.session_date)
        target = datetime.combine(session.session_date, clock_time.fromisoformat(value["start_et"]), ET)
    value["local_start"] = "下次启动：" + target.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    status_path = paths.research_root / "schedule_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    value["status_message"] = status.get("message", "")
    return value


def worker_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--research-worker"]
    return [sys.executable, "-m", "shaq_daily_oracle.desktop", "--research-worker"]


def _register_windows_worker(paths):
    # Current logged-in user only: no password prompt, elevation or SYSTEM task.
    # Task Scheduler checks once per minute; due_status remains the single ET gate.
    options = dict(capture_output=True, timeout=30, check=False, **background_process_options())
    identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], **options)
    sid = re.search(rb'\bS-1-(?:\d+-)*\d+\b', identity.stdout)
    if identity.returncode or sid is None:
        raise ValueError('无法识别当前 Windows 登录用户，未启用自动运行')
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ElementTree.register_namespace('', namespace)
    task = ElementTree.Element('{'+namespace+'}Task', version='1.3')
    def element(parent, tag, text=None, **attributes):
        node = ElementTree.SubElement(parent, '{'+namespace+'}'+tag, attributes)
        node.text = text
        return node
    triggers = element(task, 'Triggers')
    trigger = element(triggers, 'TimeTrigger')
    element(trigger, 'StartBoundary', datetime.now().astimezone().isoformat(timespec='seconds'))
    repetition = element(trigger, 'Repetition')
    element(repetition, 'Interval', 'PT1M')
    element(repetition, 'StopAtDurationEnd', 'false')
    principal = element(element(task, 'Principals'), 'Principal', id='ResearchUser')
    element(principal, 'UserId', sid.group().decode('ascii'))
    element(principal, 'LogonType', 'InteractiveToken')
    element(principal, 'RunLevel', 'LeastPrivilege')
    settings = element(task, 'Settings')
    for tag, text in [('MultipleInstancesPolicy','IgnoreNew'),
                      ('DisallowStartIfOnBatteries','false'), ('StopIfGoingOnBatteries','false'),
                      ('StartWhenAvailable','true'), ('ExecutionTimeLimit','PT0S')]:
        element(settings, tag, text)
    action = element(element(task, 'Actions', Context='ResearchUser'), 'Exec')
    command = worker_command()
    element(action, 'Command', command[0])
    element(action, 'Arguments', subprocess.list2cmdline(command[1:]))
    element(action, 'WorkingDirectory', str(paths.package_root))
    with tempfile.TemporaryDirectory(prefix='shaq-research-task-') as directory:
        definition = Path(directory) / 'task.xml'
        definition.write_bytes(ElementTree.tostring(task, encoding='utf-16', xml_declaration=True))
        result = subprocess.run(['schtasks', '/Create', '/F', '/TN', SERVICE_LABEL,
                                 '/XML', str(definition)], **options)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode('utf-8', errors='replace').strip()
        raise ValueError('Windows 自动运行注册失败：' + detail[:1000])


def save_schedule(paths, lab, submitted):
    start = clock_time.fromisoformat(str(submitted.get("start_et", "08:35")))
    if start.tzinfo is not None or not clock_time(4) <= start < clock_time(8, 50):
        raise ValueError("自动启动时间应在美东04:00至08:50之间")
    selected = submitted.get("selections", [])
    enabled = submitted.get("enabled") is True
    if enabled:
        if not selected:
            raise ValueError("请先选择自动运行的版本")
        lab._resolve_variants(selected)
        lab.settings.model_profile(submitted.get("model_profile_id"))
    value = {"enabled": enabled, "start_et": start.isoformat(), "selections": selected,
             "model_profile_id": str(submitted.get("model_profile_id", ""))}
    for key, default, maximum in [('collection_max_recoveries', 1, 3),
                                  ('collection_recovery_delay_seconds', 30, 300)]:
        previous_path = paths.research_root / 'schedule.json'
        previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
        parameter = submitted.get(key, previous.get(key, default))
        if type(parameter) is not int or not 0 <= parameter <= maximum:
            raise ValueError('行情恢复次数或等待时间设置无效')
        value[key] = parameter
    if enabled and sys.platform == 'win32':
        _register_windows_worker(paths)
    elif enabled:
        if sys.platform != "darwin":
            raise ValueError("自动运行支持 Windows 和 macOS")
        from pathlib import Path
        destination = Path.home() / "Library/LaunchAgents" / (SERVICE_LABEL + ".plist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        definition = {"Label": SERVICE_LABEL, "ProgramArguments": worker_command(),
                      "RunAtLoad": True, "StartInterval": 30,
                      "WorkingDirectory": str(paths.package_root),
                      "EnvironmentVariables": {"PYTHONPATH": str(paths.package_root / "src"),
                                               "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                      "StandardOutPath": str(paths.research_root / "schedule.stdout.log"),
                      "StandardErrorPath": str(paths.research_root / "schedule.stderr.log")}
        # Stable service identity; editing settings does not restart an active batch.
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"], capture_output=True)
        if loaded.returncode != 0:
            destination.write_bytes(plistlib.dumps(definition))
            result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], capture_output=True, text=True)
            if result.returncode:
                raise ValueError("后台启动失败：" + result.stderr.strip())
    _atomic_json(paths.research_root / "schedule.json", value)
    return schedule_status(paths)


def due_status(now, start_et):
    now = now.astimezone(ET)
    if market_session(now.date()) is None:
        return "closed"
    start = datetime.combine(now.date(), clock_time.fromisoformat(start_et), ET)
    if now < start:
        return "waiting"
    if now >= datetime.combine(now.date(), clock_time(8, 50), ET):
        return "missed"
    return "due"


def _collection_recovery_due(saved, value, now):
    """Only failed acquisition before a batch exists is safe to restart automatically."""
    if (saved.get('status') != 'failed' or saved.get('error_type') != 'ResearchCollectionError'
            or saved.get('batch_id') or not is_transient_diagnostic(saved.get('error_diagnostic'))):
        return False
    limit = value.get('collection_max_recoveries', 1)
    delay = value.get('collection_recovery_delay_seconds', 30)
    count = saved.get('collection_recovery_count', 0)
    if (type(limit) is not int or not 0 <= limit <= 3 or type(count) is not int
            or count < 0 or count >= limit or type(delay) is not int or not 0 <= delay <= 300):
        return False
    try:
        completed = datetime.fromisoformat(saved['completed_at_et'])
        return (completed.tzinfo is not None and completed.astimezone(ET).date() == now.date()
                and now >= completed + timedelta(seconds=delay))
    except (KeyError, TypeError, ValueError):
        return False


@guarded_worker
def run_research_worker(paths):
    from .lab_service import LabService
    lock = FileLock(str(paths.research_root / "schedule.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return 0
    lab = None
    state = None
    recovery_pending = False
    try:
        value = schedule_status(paths)
        lab = LabService(paths)
        if not value["enabled"]:
            return 0
        now = datetime.now(ET)
        state = due_status(now, value["start_et"])
        ledger = paths.research_root / "automatic_runs" / f"{now.date()}.json"
        if state in {"waiting", "closed"}:
            return 0
        saved = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
        delay = value.get('collection_recovery_delay_seconds', 30)
        retry_at = now + timedelta(seconds=delay if type(delay) is int and 0 <= delay <= 300 else 30)
        recovery_pending = (due_status(retry_at, value['start_et']) == 'due'
                            and _collection_recovery_due(saved, value, retry_at))
        recovering = state == 'due' and _collection_recovery_due(saved, value, now)
        if saved.get("status") in {"complete", "partial_failure", "missed", "failed"} and not recovering:
            return 0
        if state == "missed":
            _atomic_json(ledger, {"status": "missed", "recorded_at": now.isoformat()})
            _atomic_json(paths.research_root / "schedule_status.json", {"message": "错过自动运行窗口，可手动运行练习"})
            return 0
        recovery_count = saved.get('collection_recovery_count', 0)
        if recovering:
            # Keep the previous attempt intact before start_batch can replace its job status.
            receipt = ledger.parent / 'attempts' / (now.date().isoformat() + '-' + sha256_payload(saved) + '.json')
            if not receipt.exists():
                _atomic_json(receipt, saved)
            recovery_count += 1
            _atomic_json(paths.research_root / 'schedule_status.json', {
                'message': '正在恢复行情采集，复用已取得的历史行情', 'heartbeat': now.isoformat()})
        _atomic_json(ledger, {"status": "running", "started_at": now.isoformat(),
                              'collection_recovery_count': recovery_count})
        job = lab.start_batch(selections=value["selections"], model_profile_id=value["model_profile_id"])
        while True:
            current = next((x for x in lab.job_statuses() if x["job_id"] == job["job_id"]), job)
            _atomic_json(paths.research_root / "schedule_status.json", {"message": current.get("message", "运行中"), "heartbeat": datetime.now(ET).isoformat()})
            if current["status"] not in {"queued", "running"}:
                result = {**current, 'collection_recovery_count': recovery_count}
                _atomic_json(ledger, result)
                retry_at = datetime.now(ET) + timedelta(seconds=delay if type(delay) is int and 0 <= delay <= 300 else 30)
                recovery_pending = (due_status(retry_at, value['start_et']) == 'due'
                                    and _collection_recovery_due(result, value, retry_at))
                return 0
            time.sleep(5)
    except Exception as exc:
        _atomic_json(paths.research_root / 'schedule_status.json', {
            'message': '自动运行失败：' + str(exc), 'recorded_at': datetime.now(ET).isoformat(),
            'error_type': type(exc).__name__,
        })
        if 'ledger' in locals():
            _atomic_json(ledger, {'status': 'failed', 'error': str(exc),
                                  'collection_recovery_count': locals().get('recovery_count', 0)})
        return 1
    finally:
        try:
            if lab is not None:
                # First finish the time-sensitive forecast decision. Historical
                # prices must not spend its premarket collection window.
                owned = getattr(lab, '_owned_result_refresh', None)
                if owned:
                    lab.wait_result_refresh(owned[0], timeout=None)
                elif state != 'waiting' and not recovery_pending:
                    refresh = lab.refresh_labels_if_due()
                    if refresh.get('status') == 'running':
                        # Worker network calls enforce their actual deadlines.
                        # Do not abandon our daemon at the GUI's short wait limit.
                        lab.wait_result_refresh(refresh['operation_id'], timeout=None)
                    # Another process owns an already_running operation and its
                    # lifetime; this scheduler neither waits nor changes it.
        except Exception as exc:
            _atomic_json(paths.research_root / 'schedule_status.json', {
                'message': '价格与成绩刷新失败：' + str(exc),
                'recorded_at': datetime.now(ET).isoformat(), 'error_type': type(exc).__name__,
            })
        finally:
            lock.release()
