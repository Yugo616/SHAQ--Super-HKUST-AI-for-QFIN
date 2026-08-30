from __future__ import annotations

import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from .app_paths import AppPaths
from .market_calendar import market_session, next_market_session
from .settings import SettingsStore, _atomic_json
from .workflow import Workflow


class ServiceError(RuntimeError):
    """The desktop watcher could not start or register safely."""


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "shaq_daily_oracle.desktop", "--worker"]


def _write_status(paths: AppPaths, value: dict[str, Any]) -> None:
    _atomic_json(paths.runtime_root / "service_status.json", {
        "schema_version": 1,
        "updated_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        **value,
    })


def _has_completed_session(paths: AppPaths, session_date: str) -> bool:
    return any(
        (runtime / "audit_complete.json").is_file()
        for runtime in paths.runtime_root.glob(f"SHAQ-CANARY-{session_date}-*")
    )


def run_worker(*, paths: AppPaths, once: bool = False) -> int:
    paths.ensure()
    lock = FileLock(str(paths.data_root / "worker.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        _write_status(paths, {"state": "connected_to_existing_worker"})
        return 0
    store = SettingsStore(paths)
    try:
        while True:
            settings = store.load()
            if not settings.get("setup_complete"):
                _write_status(paths, {"state": "setup_required"})
                return 2 if once else 0
            if not once and not settings.get("automatic_run_enabled"):
                _write_status(paths, {"state": "automatic_run_disabled"})
                return 0
            try:
                store.apply_to_environment()
            except Exception as exc:
                _write_status(paths, {
                    "state": "configuration_error", "error_type": type(exc).__name__,
                    "message": str(exc),
                })
                return 2 if once else 0
            now_et = datetime.now(ZoneInfo("America/New_York"))
            session = market_session(now_et.date())
            if session is None:
                following = next_market_session(now_et.date())
                _write_status(paths, {
                    "state": "market_closed", "next_session": following.session_date.isoformat(),
                    "next_market_open_et": following.market_open.isoformat(),
                })
                if once:
                    return 0
                time.sleep(300)
                continue
            start_time = clock_time.fromisoformat(str(settings["automatic_start_et"]))
            start = datetime.combine(session.session_date, start_time, ZoneInfo("America/New_York"))
            if now_et < start:
                _write_status(paths, {"state": "waiting", "next_start_et": start.isoformat()})
                if once:
                    return 0
                time.sleep(min(300, max(30, int((start - now_et).total_seconds()))))
                continue
            if now_et > session.market_close + timedelta(minutes=30):
                _wait_for_next_day(paths)
                if once:
                    return 0
                continue
            if _has_completed_session(paths, session.session_date.isoformat()):
                _write_status(paths, {"state": "session_already_recorded"})
                if once:
                    return 0
                time.sleep(300)
                continue
            _write_status(paths, {"state": "workflow_running", "session": session.session_date.isoformat()})
            workflow = Workflow(
                package_root=paths.package_root,
                runtime_root=paths.runtime_root,
                host=str(settings["opend_host"]),
                port=int(settings["opend_port"]),
            )
            try:
                runtime = workflow.run(
                    requested_mode="paper", session_date=session.session_date, wait=True
                )
                _write_status(paths, {"state": "workflow_complete", "run_id": runtime.name})
            except Exception as exc:
                failure = workflow.failure_record(exc)
                _write_status(paths, {
                    "state": "workflow_failed", "error_type": type(exc).__name__,
                    "message": str(exc), "failure_id": failure.name,
                })
                if once:
                    return 2
            if once:
                return 0
            time.sleep(300)
    finally:
        lock.release()


def _wait_for_next_day(paths: AppPaths) -> None:
    following = next_market_session(datetime.now(ZoneInfo("America/New_York")).date())
    _write_status(paths, {
        "state": "session_finished", "next_session": following.session_date.isoformat(),
        "next_market_open_et": following.market_open.isoformat(),
    })
    time.sleep(300)


def start_worker(paths: AppPaths, *, once: bool = False) -> int:
    command = _worker_command() + (["--once"] if once else [])
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": paths.package_root,
        "start_new_session": True,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs.pop("start_new_session")
    process = subprocess.Popen(command, **kwargs)
    return int(process.pid)


def _set_automatic_flag(paths: AppPaths, enabled: bool) -> None:
    store = SettingsStore(paths)
    value = store.load()
    value["automatic_run_enabled"] = enabled
    _atomic_json(paths.settings_file, value)


def enable_autostart(paths: AppPaths) -> dict[str, Any]:
    _set_automatic_flag(paths, True)
    system = platform.system()
    command = _worker_command()
    if system == "Darwin":
        destination = Path.home() / "Library/LaunchAgents/com.shaq.daily-oracle.plist"
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": "com.shaq.daily-oracle",
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "StandardOutPath": str(paths.log_root / "worker.log"),
            "StandardErrorPath": str(paths.log_root / "worker-error.log"),
        }
        descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".plist")
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_bytes(plistlib.dumps(payload))
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
            capture_output=True, text=True, check=False,
        )
        start_worker(paths)
        return {"enabled": True, "scheduler": "launchd"}
    if system == "Windows":
        quoted = subprocess.list2cmdline(command)
        result = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", "SHAQ Daily Oracle",
             "/SC", "ONLOGON", "/TR", quoted],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise ServiceError(result.stderr.strip() or "Windows Task Scheduler registration failed")
        start_worker(paths)
        return {"enabled": True, "scheduler": "Task Scheduler"}
    raise ServiceError("desktop automatic running is supported on macOS and Windows")


def disable_future_runs(paths: AppPaths) -> dict[str, Any]:
    _set_automatic_flag(paths, False)
    return {
        "enabled": False,
        "message": "已停止后续自动运行；当前已开始的交易日流程不会被强行终止。",
    }
