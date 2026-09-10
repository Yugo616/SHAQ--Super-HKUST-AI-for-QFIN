"""Local research scheduler. This module never imports a broker."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from .market_calendar import market_session, next_market_session
from .settings import _atomic_json

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
    if enabled:
        if sys.platform != "darwin":
            raise ValueError("当前本机预览版的自动运行适用于macOS")
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


def run_research_worker(paths):
    from .lab_service import LabService
    lock = FileLock(str(paths.research_root / "schedule.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return 0
    try:
        value = schedule_status(paths)
        lab = LabService(paths)
        lab.refresh_labels_if_due()
        if not value["enabled"]:
            return 0
        now = datetime.now(ET)
        state = due_status(now, value["start_et"])
        ledger = paths.research_root / "automatic_runs" / f"{now.date()}.json"
        if state in {"waiting", "closed"}:
            return 0
        saved = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
        if saved.get("status") in {"complete", "partial_failure", "missed", "failed"}:
            return 0
        if state == "missed":
            _atomic_json(ledger, {"status": "missed", "recorded_at": now.isoformat()})
            _atomic_json(paths.research_root / "schedule_status.json", {"message": "错过自动运行窗口，可手动运行练习"})
            return 0
        _atomic_json(ledger, {"status": "running", "started_at": now.isoformat()})
        job = lab.start_batch(selections=value["selections"], model_profile_id=value["model_profile_id"])
        while True:
            current = next((x for x in lab.job_statuses() if x["job_id"] == job["job_id"]), job)
            _atomic_json(paths.research_root / "schedule_status.json", {"message": current.get("message", "运行中"), "heartbeat": datetime.now(ET).isoformat()})
            if current["status"] not in {"queued", "running"}:
                _atomic_json(ledger, current)
                return 0
            time.sleep(5)
    except Exception as exc:
        _atomic_json(paths.research_root / 'schedule_status.json', {
            'message': '自动运行失败：' + str(exc), 'recorded_at': datetime.now(ET).isoformat(),
            'error_type': type(exc).__name__,
        })
        if 'ledger' in locals():
            _atomic_json(ledger, {'status': 'failed', 'error': str(exc)})
        return 1
    finally:
        lock.release()
