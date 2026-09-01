from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from .app_paths import AppPaths
from .campaign import CampaignConfig, run_campaign, write_campaign_views
from .market_calendar import market_session, next_market_session
from .postmortem_runner import PostmortemRunner
from .settings import SettingsStore, _atomic_json
from .workflow import Workflow


class ServiceError(RuntimeError):
    """The desktop watcher could not start or register safely."""


def _notify(paths: AppPaths, *, title: str, message: str, category: str) -> None:
    observed = datetime.now(ZoneInfo("America/New_York"))
    token = observed.strftime("%Y%m%dT%H%M%S%f%z")
    _atomic_json(paths.runtime_root / "notifications" / f"{token}.json", {
        "schema_version": 1,
        "recorded_at_et": observed.isoformat(),
        "category": category,
        "title": title,
        "message": message,
    })
    if platform.system() == "Darwin":
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display notification "{escaped_message}" with title "{escaped_title}"'],
            capture_output=True, text=True, check=False,
        )


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


def _has_resumable_session(paths: AppPaths, session_date: str) -> bool:
    for runtime in paths.runtime_root.glob(f"SHAQ-CANARY-{session_date}-*"):
        if not (runtime / "workflow_identity.json").is_file():
            continue
        corrections = []
        for correction_path in runtime.glob("correction_*.json"):
            try:
                corrections.append(json.loads(correction_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return False
        if any(
            correction.get("backfill_allowed") is False
            or correction.get("excluded_from_professor_summary") is True
            for correction in corrections
        ):
            continue
        return True
    return False


def run_worker(
    *, paths: AppPaths, once: bool = False,
    research_companion: Callable[[], None] | None = None,
) -> int:
    paths.ensure()
    lock = FileLock(str(paths.data_root / "worker.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        _write_status(paths, {"state": "connected_to_existing_worker"})
        return 0
    store = SettingsStore(paths)
    attempted_sessions: set[str] = set()
    notified_health_failure = False
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
                matches = sorted(
                    paths.runtime_root.glob(
                        f"SHAQ-CANARY-{session.session_date.isoformat()}-*"
                    )
                )
                runtime = matches[-1]
                provisional = runtime / "postmortem/postmortem_provisional.json"
                postmortem_config = json.loads(
                    (paths.package_root / "config/postmortem.json").read_text(encoding="utf-8")
                )
                review_at = datetime.combine(
                    session.session_date,
                    clock_time.fromisoformat(
                        str(postmortem_config["provisional_capture_after_et"])
                    ),
                    ZoneInfo("America/New_York"),
                )
                if not provisional.is_file() and now_et >= review_at:
                    try:
                        PostmortemRunner(
                            package_root=paths.package_root,
                            runtime_root=paths.runtime_root,
                            host=str(settings["opend_host"]),
                            port=int(settings["opend_port"]),
                        ).run(session_date=session.session_date, phase="provisional")
                        campaign_path = paths.data_root / "campaign.json"
                        if campaign_path.is_file():
                            write_campaign_views(CampaignConfig.load(campaign_path))
                        _write_status(paths, {
                            "state": "postmortem_complete",
                            "session": session.session_date.isoformat(),
                        })
                    except Exception as exc:
                        _write_status(paths, {
                            "state": "postmortem_failed",
                            "session": session.session_date.isoformat(),
                            "error_type": type(exc).__name__, "message": str(exc),
                            "formal_prediction_effect": "none",
                        })
                    if once:
                        return 0
                    time.sleep(300)
                    continue
                if not provisional.is_file() and now_et < review_at:
                    _write_status(paths, {
                        "state": "waiting_for_postmortem",
                        "session": session.session_date.isoformat(),
                        "next_start_et": review_at.isoformat(),
                    })
                    if once:
                        return 0
                    time.sleep(min(300, max(30, int((review_at - now_et).total_seconds()))))
                    continue
                _write_status(paths, {"state": "session_already_recorded"})
                if once:
                    return 0
                time.sleep(300)
                continue
            runtime_config = json.loads(
                (paths.package_root / "config/runtime.json").read_text(encoding="utf-8")
            )
            evidence_cutoff = datetime.combine(
                session.session_date,
                clock_time.fromisoformat(str(runtime_config["evidence_cutoff"])),
                ZoneInfo(str(runtime_config["timezone"])),
            )
            if now_et > evidence_cutoff and not _has_resumable_session(
                paths, session.session_date.isoformat()
            ):
                attempted_sessions.add(session.session_date.isoformat())
                _write_status(paths, {
                    "state": "missed_daily_cutoff",
                    "session": session.session_date.isoformat(),
                    "evidence_cutoff_et": evidence_cutoff.isoformat(),
                    "orders_submitted": False,
                })
                if once:
                    return 0
                time.sleep(300)
                continue
            if session.session_date.isoformat() in attempted_sessions:
                _write_status(paths, {
                    "state": "session_failed_no_in_process_retry",
                    "session": session.session_date.isoformat(),
                })
                if once:
                    return 2
                time.sleep(300)
                continue
            _write_status(paths, {"state": "workflow_running", "session": session.session_date.isoformat()})
            attempted_sessions.add(session.session_date.isoformat())
            campaign_path = paths.data_root / "campaign.json"
            if campaign_path.is_file():
                campaign = CampaignConfig.load(campaign_path)
                if session.session_date in campaign.session_dates:
                    reliability = json.loads(
                        (paths.package_root / "config/reliability.json").read_text(encoding="utf-8")
                    )
                    notification_title = str(reliability["notification_title"])
                    companion_error: list[Exception] = []
                    companion_thread = None
                    if research_companion is not None:
                        def run_companion() -> None:
                            try:
                                research_companion()
                            except Exception as exc:
                                companion_error.append(exc)
                                observed = datetime.now(ZoneInfo("America/New_York"))
                                _atomic_json(paths.runtime_root / "shadow_failure_latest.json", {
                                    "schema_version": 1,
                                    "recorded_at_et": observed.isoformat(),
                                    "session": session.session_date.isoformat(),
                                    "error_type": type(exc).__name__,
                                    "message": str(exc),
                                    "formal_prediction_effect": "none",
                                    "orders_submitted": False,
                                })
                                _notify(
                                    paths, title=notification_title,
                                    message="研究Shadow失败；正式版不受影响。",
                                    category="shadow_failed",
                                )

                        companion_thread = threading.Thread(
                            target=run_companion,
                            name="daily-oracle-research-companion",
                            daemon=False,
                        )
                        companion_thread.start()
                    campaign_failed = False

                    def observe_preflight(preflight: dict[str, Any]) -> None:
                        nonlocal notified_health_failure
                        failed = [
                            name for name, passed in preflight.get("checks", {}).items()
                            if not passed
                        ]
                        if failed and not notified_health_failure:
                            notified_health_failure = True
                            _notify(
                                paths, title=notification_title,
                                message="盘前检查失败：" + "、".join(failed) + "；尚未下单。",
                                category="health_gate_failed",
                            )
                        elif not failed and notified_health_failure:
                            notified_health_failure = False
                            _notify(
                                paths, title=notification_title,
                                message="盘前检查已恢复，正式流程可以继续。",
                                category="health_gate_recovered",
                            )
                    try:
                        result = run_campaign(
                            package_root=paths.package_root,
                            config=campaign,
                            preflight_observer=observe_preflight,
                        )
                        if result != 0:
                            raise ServiceError(f"campaign returned fail-closed status {result}")
                        _write_status(paths, {
                            "state": "workflow_complete", "session": session.session_date.isoformat(),
                        })
                    except Exception as exc:
                        campaign_failed = True
                        incident_path = paths.runtime_root / "campaign_failure_latest.json"
                        incident = (
                            json.loads(incident_path.read_text(encoding="utf-8"))
                            if incident_path.is_file() else {}
                        )
                        failed_stage = str(incident.get("stage") or type(exc).__name__)
                        _write_status(paths, {
                            "state": "workflow_failed", "error_type": type(exc).__name__,
                            "message": str(exc), "session": session.session_date.isoformat(),
                            "stage": failed_stage,
                            "orders_submitted": bool(
                                incident.get("impact", {}).get("orders_submitted", False)
                            ),
                        })
                        _notify(
                            paths, title=notification_title,
                            message=f"正式流程在{failed_stage}失败；请查看仪表盘，系统不会补票。",
                            category="formal_workflow_failed",
                        )
                    if companion_thread is not None:
                        companion_thread.join(timeout=5)
                    if companion_error:
                        error = companion_error[0]
                        _write_status(paths, {
                            "state": "research_companion_failed",
                            "error_type": type(error).__name__, "message": str(error),
                            "session": session.session_date.isoformat(),
                            "formal_prediction_effect": "none",
                        })
                    if once:
                        return 2 if campaign_failed else 0
                    time.sleep(300)
                    continue
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
        settings = SettingsStore(paths).load()
        if settings.get("ai_backend") == "codex-cli":
            codex = shutil.which("codex")
            if not codex:
                raise ServiceError("Codex CLI is unavailable")
            payload["EnvironmentVariables"] = {
                "PATH": ":".join((
                    str(Path(codex).resolve().parent),
                    str(Path(sys.executable).resolve().parent),
                    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
                )),
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
            ["launchctl", "bootout", f"gui/{os.getuid()}",
             str(Path.home() / "Library/LaunchAgents/com.shaq.dailyoracle.campaign.plist")],
            capture_output=True, text=True, check=False,
        )
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(destination)],
            capture_output=True, text=True, check=False,
        )
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
