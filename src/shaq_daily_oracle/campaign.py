from __future__ import annotations

import csv
import errno
import fcntl
import html
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .execution import select_simulate_us_account
from .identity import ensure_formal_core_lock
from .postmortem_runner import PostmortemRunner
from .workflow import Workflow, _resolve_universe


class CampaignError(RuntimeError):
    """The local paper-trading campaign cannot continue safely."""


class CampaignAlreadyRunning(CampaignError):
    """A second scheduler process found the active campaign process."""


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    session_dates: tuple[date, ...]
    runtime_root: Path
    workflow_start_et: clock_time
    heartbeat_seconds: int
    active_health_seconds: int
    idle_health_seconds: int
    host: str
    port: int

    @classmethod
    def load(cls, path: Path) -> "CampaignConfig":
        value = _read(path)
        dates = tuple(date.fromisoformat(item) for item in value["session_dates"])
        if not dates or len(dates) != len(set(dates)) or dates != tuple(sorted(dates)):
            raise CampaignError("campaign dates must be unique, nonempty and ordered")
        if any(item.weekday() >= 5 for item in dates):
            raise CampaignError("campaign dates must be weekdays")
        intervals = {
            "heartbeat_seconds": int(value["heartbeat_seconds"]),
            "active_health_seconds": int(value["active_health_seconds"]),
            "idle_health_seconds": int(value["idle_health_seconds"]),
        }
        if not 10 <= intervals["heartbeat_seconds"] <= 60:
            raise CampaignError("heartbeat interval must be from 10 to 60 seconds")
        if intervals["active_health_seconds"] < intervals["heartbeat_seconds"]:
            raise CampaignError("active health interval cannot be shorter than heartbeat")
        if intervals["idle_health_seconds"] < intervals["active_health_seconds"]:
            raise CampaignError("idle health interval cannot be shorter than active health")
        return cls(
            campaign_id=str(value["campaign_id"]),
            session_dates=dates,
            runtime_root=Path(value["runtime_root"]).expanduser().resolve(),
            workflow_start_et=clock_time.fromisoformat(value["workflow_start_et"]),
            heartbeat_seconds=intervals["heartbeat_seconds"],
            active_health_seconds=intervals["active_health_seconds"],
            idle_health_seconds=intervals["idle_health_seconds"],
            host=str(value.get("opend_host", "127.0.0.1")),
            port=int(value.get("opend_port", 11111)),
        )


@contextmanager
def campaign_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}:
                raise CampaignAlreadyRunning("another campaign process is already active") from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _sec_ready() -> bool:
    identity = os.environ.get("DAILY_ORACLE_SEC_USER_AGENT", "").strip()
    if not identity:
        return False
    request = urllib.request.Request(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": identity, "Accept-Encoding": "gzip, deflate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 200 and bool(response.read(64))
    except Exception:
        return False


class Heartbeat:
    def __init__(self, config: CampaignConfig, *, session_date: date) -> None:
        self.config = config
        self.session_date = session_date
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.stage = "starting"
        self.last_health_at = 0.0
        self.last_health = False

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def _active(self, now_et: datetime) -> bool:
        minute = now_et.hour * 60 + now_et.minute
        return 8 * 60 + 15 <= minute <= 9 * 60 + 40 or 15 * 60 + 50 <= minute <= 16 * 60 + 6

    def _run(self) -> None:
        zone = ZoneInfo("America/New_York")
        while not self.stop_event.is_set():
            now_et = datetime.now(zone)
            health_interval = (
                self.config.active_health_seconds if self._active(now_et)
                else self.config.idle_health_seconds
            )
            if time.monotonic() - self.last_health_at >= health_interval:
                self.last_health = _tcp_ready(self.config.host, self.config.port)
                self.last_health_at = time.monotonic()
            _write(self.config.runtime_root / "service_status.json", {
                "schema_version": 1,
                "campaign_id": self.config.campaign_id,
                "session_date": self.session_date.isoformat(),
                "process_id": os.getpid(),
                "stage": self.stage,
                "heartbeat_at_et": now_et.isoformat(),
                "opend_reachable": self.last_health,
            })
            self.stop_event.wait(self.config.heartbeat_seconds)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="daily-oracle-heartbeat", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)


def run_preflight(*, package_root: Path, config: CampaignConfig) -> dict[str, Any]:
    checks: dict[str, bool] = {
        "sec_identity_configured": bool(os.environ.get("DAILY_ORACLE_SEC_USER_AGENT", "").strip()),
        "sec_access": _sec_ready(),
        "opend_reachable": _tcp_ready(config.host, config.port),
        "codex_available": shutil.which("codex") is not None,
        "codex_isolation": False,
        "universe_available": False,
        "single_us_simulate_account": False,
        "release_tests_passed": False,
        "formal_core_frozen": False,
    }
    try:
        identity = _read(package_root / "config/system-identity.json")["identity"]
        ensure_formal_core_lock(
            package_root=package_root,
            runtime_root=config.runtime_root,
            system_identity=identity,
            freeze_start=config.session_dates[0],
            freeze_end=config.session_dates[-1],
            observed_at=datetime.now(ZoneInfo("America/New_York")),
        )
        checks["formal_core_frozen"] = True
    except Exception:
        pass
    try:
        _resolve_universe(config.runtime_root)
        checks["universe_available"] = True
    except Exception:
        pass
    if checks["opend_reachable"]:
        try:
            from futu import OpenSecTradeContext, RET_OK, TrdMarket  # type: ignore

            trade = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US, host=config.host, port=config.port
            )
            try:
                result, frame = trade.get_acc_list()
                if result == RET_OK:
                    select_simulate_us_account([row.to_dict() for _, row in frame.iterrows()])
                    checks["single_us_simulate_account"] = True
            finally:
                trade.close()
        except Exception:
            pass
    with tempfile.TemporaryDirectory(prefix="shaq-preflight-") as temporary:
        isolation_path = Path(temporary) / "isolation_status.json"
        attestation_path = Path(temporary) / "isolation_attestation.json"
        isolation_result = subprocess.run(
            [sys.executable, str(package_root / "scripts/snapshot_isolation.py"),
             "--output", str(isolation_path), "--backend", "sandboxed-codex",
             "--workspace-root", str(package_root.parent),
             "--attestation", str(attestation_path)],
            cwd=package_root, capture_output=True, text=True, check=False,
        )
        if isolation_result.returncode == 0 and isolation_path.exists():
            checks["codex_isolation"] = _read(isolation_path).get("formal_ai_enabled") is True
    with tempfile.TemporaryDirectory(prefix="shaq-tests-") as temporary:
        report_path = Path(temporary) / "acceptance.json"
        result = subprocess.run(
            [sys.executable, str(package_root / "scripts/run_acceptance_tests.py"),
             "--output", str(report_path)],
            cwd=package_root, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and report_path.exists():
            report = _read(report_path)
            checks["release_tests_passed"] = bool(
                report.get("public_passed") and report.get("release_validator_passed")
            )
            _write(config.runtime_root / "campaign_preflight_tests.json", report)
    value = {
        "schema_version": 1,
        "campaign_id": config.campaign_id,
        "checked_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "checks": checks,
        "paper_allowed": all(checks.values()),
    }
    _write(config.runtime_root / "campaign_preflight.json", value)
    return value


def campaign_rows(config: CampaignConfig) -> list[dict[str, Any]]:
    rows = []
    for session_date in config.session_dates:
        matches = sorted(config.runtime_root.glob(f"SHAQ-CANARY-{session_date.isoformat()}-*"))
        runtime = matches[-1] if matches else None
        row: dict[str, Any] = {
            "交易日": session_date.isoformat(), "运行状态": "尚未运行",
            "正式运行": "否",
            "正式预测数": 0, "命中数": 0, "已评价数": 0,
            "模拟盈亏": None, "费用": None, "复盘状态": "尚未复盘",
            "复盘候选数": 0, "未覆盖候选数": 0,
            "系统身份": "", "正式核心哈希": "", "异常": "",
        }
        if runtime:
            row["运行状态"] = "已完成" if (runtime / "audit_complete.json").exists() else "运行中或安全停止"
            if (runtime / "frozen_run.json").exists():
                frozen = _read(runtime / "frozen_run.json")
                if frozen.get("mode") == "canary":
                    row["正式运行"] = "是"
                    row["正式预测数"] = len(frozen.get("predictions", []))
                row["系统身份"] = frozen.get("system_identity", "")
            lock = config.runtime_root / "formal_core_lock.json"
            if lock.is_file():
                row["正式核心哈希"] = _read(lock).get("formal_core_sha256", "")
            if (runtime / "evaluations_provisional.json").exists():
                evaluations = _read(runtime / "evaluations_provisional.json").get("evaluations", [])
                row["已评价数"] = len(evaluations)
                row["命中数"] = sum(item.get("correct") is True for item in evaluations)
            if (runtime / "execution_ledger.json").exists():
                trips = _read(runtime / "execution_ledger.json").get("round_trips", [])
                profits = [item.get("net_pnl") for item in trips if item.get("net_pnl") is not None]
                fees = [item.get("fees") for item in trips if item.get("fees") is not None]
                row["模拟盈亏"] = sum(profits) if profits else None
                row["费用"] = sum(float(value) for value in fees) if fees else None
            if (runtime / "workflow_failure.json").exists():
                row["异常"] = _read(runtime / "workflow_failure.json").get("error_type", "安全停止")
            post_root = runtime / "postmortem"
            post_path = (
                post_root / "postmortem_final.json"
                if (post_root / "postmortem_final.json").is_file()
                else post_root / "postmortem_provisional.json"
            )
            if post_path.is_file():
                review = _read(post_path)
                row["复盘状态"] = "最终" if review.get("phase") == "final" else "暂定"
                row["复盘候选数"] = int(review.get("candidate_count", 0))
                row["未覆盖候选数"] = sum(
                    item.get("uncovered_realized_move") is True
                    for item in review.get("candidate_diagnostics", [])
                )
        rows.append(row)
    return rows


def write_campaign_views(config: CampaignConfig) -> None:
    rows = campaign_rows(config)
    completed = sum(row["运行状态"] == "已完成" for row in rows)
    formal_runs = sum(row["正式运行"] == "是" for row in rows)
    evaluated = sum(int(row["已评价数"]) for row in rows)
    correct = sum(int(row["命中数"]) for row in rows)
    summary = {
        "campaign_id": config.campaign_id,
        "date_start": config.session_dates[0].isoformat(),
        "date_end": config.session_dates[-1].isoformat(),
        "scheduled_sessions": len(config.session_dates),
        "completed_sessions": completed,
        "formal_runs": formal_runs,
        "empty_runs": sum(
            row["正式运行"] == "是" and row["正式预测数"] == 0 for row in rows
        ),
        "evaluated_predictions": evaluated,
        "correct_predictions": correct,
        "directional_accuracy": correct / evaluated if evaluated else None,
        "paper_net_pnl": sum(float(row["模拟盈亏"] or 0) for row in rows),
        "fees": sum(float(row["费用"] or 0) for row in rows),
        "exception_sessions": sum(bool(row["异常"]) for row in rows),
        "postmortem_sessions": sum(row["复盘状态"] in {"暂定", "最终"} for row in rows),
        "final_postmortem_sessions": sum(row["复盘状态"] == "最终" for row in rows),
        "identity_groups": [],
    }
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row["正式运行"] != "是":
            continue
        key = (str(row["系统身份"]), str(row["正式核心哈希"]))
        group = groups.setdefault(key, {
            "system_identity": key[0], "formal_core_sha256": key[1],
            "formal_runs": 0, "predictions": 0, "evaluated": 0, "correct": 0,
        })
        group["formal_runs"] += 1
        group["predictions"] += int(row["正式预测数"])
        group["evaluated"] += int(row["已评价数"])
        group["correct"] += int(row["命中数"])
    summary["identity_groups"] = [groups[key] for key in sorted(groups)]
    _write(config.runtime_root / "campaign_summary.json", summary)
    csv_path = config.runtime_root / "campaign_summary.csv"
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    _write_text(csv_path, "\ufeff" + csv_buffer.getvalue())
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else '—'))}</td>" for value in row.values()) + "</tr>"
        for row in rows
    )
    headers = "".join(f"<th>{html.escape(key)}</th>" for key in rows[0])
    page = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>SHAQ Daily Oracle 连续模拟进度</title>"
        "<style>body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;margin:36px;color:#172033}table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #d9deea;text-align:left}small{color:#647086}</style></head><body>"
        f"<h1>SHAQ Daily Oracle 连续模拟进度</h1><small>{html.escape(config.campaign_id)}</small>"
        f"<table><thead><tr>{headers}</tr></thead><tbody>{body_rows}</tbody></table></body></html>"
    )
    _write_text(config.runtime_root / "campaign_status.html", page)


def build_final_summary_ppt(*, package_root: Path, config: CampaignConfig) -> Path:
    node = os.environ.get("RUNTIME_NODE", "")
    modules = os.environ.get("RUNTIME_NODE_MODULES", "")
    if not Path(node).is_file() or not Path(modules).is_dir():
        raise CampaignError("the verified presentation runtime is unavailable")
    link = package_root / "node_modules"
    if link.is_symlink() and link.resolve() != Path(modules).resolve():
        link.unlink()
    if not link.exists():
        link.symlink_to(Path(modules), target_is_directory=True)
    output = config.runtime_root / "SHAQ_连续模拟九日汇总.pptx"
    result = subprocess.run(
        [node, str(package_root / "scripts/build_campaign_summary.mjs"),
         str(config.runtime_root / "campaign_summary.json"), str(output)],
        cwd=package_root, capture_output=True, text=True, check=False,
    )
    if result.returncode or not output.is_file():
        raise CampaignError("final summary presentation generation failed")
    return output


def _record_campaign_failure(
    *, config: CampaignConfig, stage: str, error: Exception
) -> None:
    observed = datetime.now(ZoneInfo("America/New_York"))
    payload = {
        "schema_version": 1,
        "campaign_id": config.campaign_id,
        "status": "fail_closed",
        "stage": stage,
        "recorded_at_et": observed.isoformat(),
        "process_id": os.getpid(),
        "error_type": type(error).__name__,
        "message": str(error),
        "traceback": traceback.format_exc(),
    }
    failure_root = config.runtime_root / "campaign_failures"
    name = f"{observed.strftime('%Y%m%dT%H%M%S%f%z')}-{os.getpid()}.json"
    try:
        _write(failure_root / name, payload)
        _write(config.runtime_root / "campaign_failure_latest.json", payload)
    except Exception:
        pass


def run_campaign(*, package_root: Path, config: CampaignConfig, preflight_only: bool = False) -> int:
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if not preflight_only and today_et not in config.session_dates:
        return 0
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    stage = "acquire_campaign_lock"
    try:
        with campaign_lock(config.runtime_root / "campaign.lock"):
            previous_reviews = []
            for value in config.session_dates:
                if value >= today_et:
                    continue
                matches = sorted(config.runtime_root.glob(f"SHAQ-CANARY-{value.isoformat()}-*"))
                if not matches:
                    continue
                post_root = matches[-1] / "postmortem"
                if (
                    (post_root / "postmortem_provisional.json").is_file()
                    and not (post_root / "postmortem_final.json").exists()
                ):
                    previous_reviews.append(value)
            if previous_reviews:
                try:
                    PostmortemRunner(
                        package_root=package_root, runtime_root=config.runtime_root,
                        host=config.host, port=config.port,
                    ).run(session_date=previous_reviews[-1], phase="final")
                except Exception as exc:
                    _write(config.runtime_root / "postmortem_finalization_failure.json", {
                        "schema_version": 1,
                        "session_date": previous_reviews[-1].isoformat(),
                        "recorded_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "formal_prediction_effect": "none",
                    })
            stage = "preflight"
            preflight = run_preflight(package_root=package_root, config=config)
            write_campaign_views(config)
            if preflight_only:
                return 0 if preflight["paper_allowed"] else 2
            heartbeat = Heartbeat(config, session_date=today_et)
            heartbeat.start()
            try:
                start_at = datetime.combine(
                    today_et, config.workflow_start_et, ZoneInfo("America/New_York")
                )
                stage = "wait_for_workflow_start"
                heartbeat.set_stage("等待盘前流程开始")
                while datetime.now(ZoneInfo("America/New_York")) < start_at:
                    remaining = (
                        start_at - datetime.now(ZoneInfo("America/New_York"))
                    ).total_seconds()
                    time.sleep(min(config.heartbeat_seconds, remaining))
                stage = "workflow"
                heartbeat.set_stage("运行六领域分析与模拟盘")
                workflow = Workflow(
                    package_root=package_root,
                    runtime_root=config.runtime_root,
                    host=config.host,
                    port=config.port,
                )
                requested_mode = "paper" if preflight["paper_allowed"] else "shadow"
                try:
                    workflow.run(
                        requested_mode=requested_mode,
                        session_date=today_et,
                        wait=True,
                    )
                except Exception as exc:
                    workflow.failure_record(exc)
                    raise
                stage = "daily_report"
                heartbeat.set_stage("生成当日审计与汇总")
                write_campaign_views(config)
                if today_et == config.session_dates[-1]:
                    stage = "final_summary"
                    build_final_summary_ppt(package_root=package_root, config=config)
                return 0
            finally:
                heartbeat.close()
    except CampaignAlreadyRunning:
        return 0
    except Exception as exc:
        _record_campaign_failure(config=config, stage=stage, error=exc)
        raise
