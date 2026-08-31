from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .app_paths import app_paths, migrate_legacy_runtime
from .dashboard import DashboardIndex
from .execution import select_simulate_us_account
from .market_calendar import market_session, next_market_session
from .service import disable_future_runs, enable_autostart, run_worker, start_worker
from .settings import SettingsError, SettingsStore, _atomic_json
from .sandboxed_codex import attest_sandboxed_codex


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class DesktopBridge:
    def __init__(self) -> None:
        self.paths = app_paths().ensure()
        migrate_legacy_runtime(self.paths)
        self.store = SettingsStore(self.paths)
        self.index = DashboardIndex(
            runtime_root=self.paths.runtime_root, database=self.paths.dashboard_db
        )
        self.window = None

    @staticmethod
    def _result(action, *args, **kwargs) -> dict[str, Any]:
        try:
            return {"ok": True, "value": action(*args, **kwargs)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    def get_state(self) -> dict[str, Any]:
        settings = self.store.load()
        public_settings = dict(settings)
        public_settings["openai_key_saved"] = bool(self.store.get_openai_key())
        if not public_settings.get("universe_file"):
            candidates = sorted(
                self.paths.runtime_root.glob("*/universe/effective_universe_formal.csv"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            public_settings["suggested_universe_file"] = str(candidates[0]) if candidates else ""
        return self._result(lambda: {
            "settings": public_settings,
            "dashboard": self.index.overview(),
            "calendar": self.calendar_status(),
        })

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._result(self.index.run_detail, run_id)

    def save_setup(self, submitted: dict[str, Any]) -> dict[str, Any]:
        def save_and_check() -> dict[str, Any]:
            saved = self.store.save_setup(submitted)
            checks = self._doctor_checks(saved)
            required = (
                "ai_model_ready", "ai_isolation_ready", "opend_reachable",
                "simulate_account_ready", "universe_available",
            )
            if not all(checks.get(name) is True for name in required):
                saved["setup_complete"] = False
                _atomic_json(self.paths.settings_file, saved)
                raise SettingsError("连接检查没有全部通过，请根据系统健康提示修正后再保存")
            return {"settings": saved, "checks": checks}
        return self._result(save_and_check)

    def choose_universe(self) -> dict[str, Any]:
        def choose() -> dict[str, Any]:
            if self.window is None:
                raise SettingsError("桌面窗口尚未准备好")
            import webview  # type: ignore

            selected = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("CSV files (*.csv)",),
            )
            return {"file": selected[0] if selected else ""}
        return self._result(choose)

    def run_today(self) -> dict[str, Any]:
        return self._result(lambda: {"worker_pid": start_worker(self.paths, once=True)})

    def enable_automatic(self) -> dict[str, Any]:
        return self._result(enable_autostart, self.paths)

    def disable_automatic(self) -> dict[str, Any]:
        return self._result(disable_future_runs, self.paths)

    def calendar_status(self) -> dict[str, Any]:
        now = datetime.now(ZoneInfo("America/New_York"))
        session = market_session(now.date())
        if session:
            return {
                "is_session": True, "session_date": session.session_date.isoformat(),
                "market_open_et": session.market_open.isoformat(),
                "market_close_et": session.market_close.isoformat(),
                "early_close": session.early_close,
            }
        following = next_market_session(now.date())
        return {
            "is_session": False, "next_session": following.session_date.isoformat(),
            "next_market_open_et": following.market_open.isoformat(),
        }

    def _doctor_checks(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = settings or self.store.load()
        model_ready = False
        isolation_ready = False
        model_error = None
        if settings.get("ai_backend") == "openai-responses" and self.store.get_openai_key():
            try:
                from openai import OpenAI  # type: ignore

                OpenAI(
                    api_key=self.store.get_openai_key(), max_retries=0, timeout=10
                ).models.retrieve(str(settings["model"]))
                model_ready = True
                isolation_ready = True
            except Exception as exc:
                model_error = f"{type(exc).__name__}: {exc}"
        elif settings.get("ai_backend") == "codex-cli":
            try:
                with tempfile.TemporaryDirectory(prefix="shaq-codex-doctor-") as name:
                    artifact = attest_sandboxed_codex(
                        workspace_root=self.paths.package_root.parent,
                        output=Path(name) / "attestation.json",
                    )
                model_ready = True
                isolation_ready = artifact["status"]["formal_ai_enabled"] is True
            except Exception as exc:
                model_error = f"{type(exc).__name__}: {exc}"
        universe = Path(str(settings.get("universe_file", ""))).expanduser()
        opend_ready = _tcp_ready(
            str(settings.get("opend_host", "127.0.0.1")),
            int(settings.get("opend_port", 11111)),
        )
        simulate_ready = False
        account_error = None
        if opend_ready:
            try:
                from futu import OpenSecTradeContext, RET_OK, TrdMarket  # type: ignore

                trade = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.US,
                    host=str(settings.get("opend_host", "127.0.0.1")),
                    port=int(settings.get("opend_port", 11111)),
                )
                try:
                    result, frame = trade.get_acc_list()
                    if result != RET_OK:
                        raise SettingsError("富途账户列表读取失败")
                    select_simulate_us_account(
                        [row.to_dict() for _, row in frame.iterrows()]
                    )
                    simulate_ready = True
                finally:
                    trade.close()
            except Exception as exc:
                account_error = f"{type(exc).__name__}: {exc}"
        return {
            "setup_complete": settings.get("setup_complete") is True,
            "ai_model_ready": model_ready,
            "ai_isolation_ready": isolation_ready,
            "ai_model_error": model_error,
            "opend_reachable": opend_ready,
            "simulate_account_ready": simulate_ready,
            "simulate_account_error": account_error,
            "universe_available": universe.is_file(),
            "calendar": self.calendar_status(),
            "automatic_run_enabled": settings.get("automatic_run_enabled") is True,
        }

    def doctor(self) -> dict[str, Any]:
        return self._result(self._doctor_checks)

    def export_report(self) -> dict[str, Any]:
        destination = self.paths.data_root / "exports" / "SHAQ_Daily_Oracle_教授报告.html"
        return self._result(lambda: {
            "file": str(self.index.export_professor_report(destination)),
        })

    def open_run_file(self, run_id: str, name: str) -> dict[str, Any]:
        if name not in {"run_replay.html", "professor_report.html", "agent_trace.html"}:
            return {"ok": False, "error": "不允许打开该文件"}
        runtime = (self.paths.runtime_root / Path(run_id).name).resolve()
        target = runtime / name
        if not target.is_file():
            return {"ok": False, "error": "该运行还没有生成这个页面"}
        if self.window is not None:
            self.window.load_url(target.as_uri())
        return {"ok": True, "value": {"opened": name}}


def launch_desktop() -> int:
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise SettingsError("桌面组件尚未安装，请安装 desktop 依赖") from exc
    bridge = DesktopBridge()
    page = Path(__file__).with_name("desktop") / "index.html"
    if not page.is_file():
        raise FileNotFoundError("desktop interface asset is missing")
    window = webview.create_window(
        "SHAQ Daily Oracle",
        page.as_uri(),
        js_api=bridge,
        width=1320,
        height=860,
        min_size=(980, 680),
        background_color="#f4f7fb",
    )
    bridge.window = window
    webview.start(debug=False, private_mode=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args, _ = parser.parse_known_args(argv)
    if args.smoke:
        paths = app_paths()
        checks = {
            "desktop_asset": (Path(__file__).with_name("desktop") / "index.html").is_file(),
            "runtime_config": (paths.package_root / "config/runtime.json").is_file(),
            "skills": len(list((paths.package_root / "skills").glob("*/SKILL.md"))) == 8,
        }
        print(json.dumps({"status": "passed" if all(checks.values()) else "failed", "checks": checks}))
        return 0 if all(checks.values()) else 2
    if args.worker:
        return run_worker(paths=app_paths().ensure(), once=args.once)
    return launch_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
