from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import webbrowser
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .app_paths import app_paths, migrate_legacy_runtime
from .dashboard import DashboardIndex
from .execution import select_simulate_us_account
from .lab_service import LabService
from .market_calendar import market_session, next_market_session
from .service import disable_future_runs, enable_autostart, run_worker, start_worker
from .settings import SettingsError, SettingsStore, _atomic_json
from .sandboxed_codex import attest_sandboxed_codex
from .update_admission import gate_for, guarded_method, WorkerAdmission


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class DesktopBridge:
    @guarded_method
    def __init__(self, paths=None) -> None:
        self.paths = (paths or app_paths()).ensure()
        self._runtime_admission = WorkerAdmission(self.paths)
        self._startup_confirmation_pending = gate_for(self.paths).startup_allowed()
        migrate_legacy_runtime(self.paths)
        self.store = SettingsStore(self.paths)
        self.index = DashboardIndex(
            runtime_root=self.paths.runtime_root, database=self.paths.dashboard_db
        )
        self.lab = LabService(self.paths)
        self.operator_status = {
            "platform_supported": sys.platform == "darwin",
            "safety_ready": False,
            "requires_separate_setup": True,
        }
        self.window = None
        self._software_updater()

    def confirm_desktop_ready(self):
        """Called only after the first successful data load and UI render."""
        def confirm():
            if self._startup_confirmation_pending:
                gate = gate_for(self.paths)
                self._runtime_admission.assert_current()
                if not gate.startup_allowed():
                    raise SettingsError('更新启动确认已失效，请重新打开应用')
                from .app_paths import application_version
                gate.finish_restart(application_version(self.paths.package_root))
                self._startup_confirmation_pending = False
            self._software_updater().start_automatic_checks()
            return {'ready':True}
        return self._result(confirm, _update_control=True)

    def _result(self, action, *args, _update_control=False, **kwargs) -> dict[str, Any]:
        try:
            # All bridge calls can trigger index/setting writes, including reads.
            admission = gate_for(self.paths).work() if hasattr(self, 'paths') and not _update_control else nullcontext()
            if not _update_control and hasattr(self, '_runtime_admission'):
                admission = self._runtime_admission.work()
            with admission:
                return {"ok": True, "value": action(*args, **kwargs)}
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            diagnostic = getattr(exc, "diagnostic", None)
            if isinstance(diagnostic, dict):
                result["diagnostic"] = diagnostic
            return result

    def get_state(self) -> dict[str, Any]:
        settings = self.store.load()
        public_settings = dict(settings)
        public_settings["openai_key_saved"] = (
            bool(settings.get("openai_key_saved"))
            or bool(os.environ.get("OPENAI_API_KEY", "").strip())
        )
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

    def get_lab_state(self) -> dict[str, Any]:
        def state() -> dict[str, Any]:
            from .operator_control import status
            value = self.lab.state()
            value["operator_mode"] = dict(self.operator_status)
            value["formal_operator"] = status(self.paths)
            return value
        return self._result(state)

    def copy_local_version(self, version_id: str, author: str) -> dict[str, Any]:
        return self._result(self.lab.copy_local_version, version_id=version_id, author=author)

    def get_module_document(self, module, version_id, author, draft_id=""):
        return self._result(self.lab.module_document, module=module, version_id=version_id, author=author, draft_id=draft_id)

    def save_module_draft(self, module, script, cases, draft_id):
        return self._result(self.lab.save_module_draft, module=module, script=script, cases=cases, draft_id=draft_id)

    def get_local_draft(self, draft_id):
        return self._result(self.lab.get_local_draft, draft_id)

    def finalize_local_version(self, draft_id: str, description: str) -> dict[str, Any]:
        return self._result(self.lab.finalize_local_version, draft_id=draft_id, description=description)

    def get_research_schedule(self) -> dict[str, Any]:
        from .research_schedule import schedule_status
        return self._result(schedule_status, self.paths)

    def save_research_schedule(self, value: dict[str, Any]) -> dict[str, Any]:
        from .research_schedule import save_schedule
        return self._result(save_schedule, self.paths, self.lab, value)

    def refresh_prices_and_results(self) -> dict[str, Any]:
        return self._result(self.lab.start_result_refresh, manual=True)

    def retry_failed_results(self) -> dict[str, Any]:
        return self._result(self.lab.start_result_refresh, manual=True, retry_failed_only=True)

    def check_result_refresh_due(self) -> dict[str, Any]:
        """Local due check; network work starts only for an eligible date."""
        return self._result(self.lab.start_result_refresh, manual=False)

    def check_operator_mode(self) -> dict[str, Any]:
        def check() -> dict[str, Any]:
            if sys.platform != "darwin":
                return {"platform_supported": False, "safety_ready": False}
            checks = self._doctor_checks()
            ready = all(checks.get(name) is True for name in (
                "ai_model_ready", "ai_isolation_ready", "opend_reachable",
                "simulate_account_ready", "universe_available",
            ))
            result = {
                "platform_supported": True, "safety_ready": ready,
                "requires_separate_setup": True, "checks": checks,
            }
            self.operator_status = result
            return result
        return self._result(check)

    def save_lab_model_profile(
        self, profile: dict[str, Any], secret: str, probe: bool = True
    ) -> dict[str, Any]:
        return self._result(
            self.lab.save_model_profile, profile, secret=secret, probe=probe
        )

    def begin_local_model_login(self, protocol: str) -> dict[str, Any]:
        def login() -> dict[str, str]:
            from .model_backends import (
                ModelProfile,
                begin_local_subscription_login,
            )

            if protocol not in {"codex-cli", "claude-code"}:
                raise SettingsError("请选择 Codex 或 Claude Code 本机订阅")
            profile = ModelProfile(
                profile_id="login-check",
                protocol=protocol,
                base_url="",
                model="subscription-default",
            )
            return begin_local_subscription_login(profile)

        return self._result(login)

    def save_lab_setup(self, submitted: dict[str, Any]) -> dict[str, Any]:
        return self._result(self.lab.save_setup, submitted)

    def check_research_environment(self) -> dict[str, Any]:
        return self._result(self.lab.check_research_environment)

    def begin_github_login(self) -> dict[str, Any]:
        return self._result(self.lab.begin_github_login)

    def complete_github_login(self, device_code: str) -> dict[str, Any]:
        return self._result(self.lab.complete_github_login, device_code)

    def open_external_url(self, url: str) -> dict[str, Any]:
        def open_safe() -> dict[str, Any]:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc not in {
                "github.com", "www.github.com"
            }:
                raise SettingsError("只允许打开GitHub登录页面")
            return {"opened": bool(webbrowser.open(url))}
        return self._result(open_safe)

    def open_model_installation(self, protocol: str) -> dict[str, Any]:
        def open_installation() -> dict[str, Any]:
            pages = {
                "codex-cli": "https://developers.openai.com/codex/cli/",
                "claude-code": "https://code.claude.com/docs/en/setup",
            }
            if protocol not in pages:
                raise SettingsError("请选择 Codex 或 Claude Code 的官方安装说明")
            return {"opened": bool(webbrowser.open(pages[protocol]))}
        return self._result(open_installation)

    def check_software_update(self) -> dict[str, Any]:
        def check():
            value = self._software_updater().check()
            self._offered_software_release = value
            return value
        return self._result(check, _update_control=True)

    def _software_updater(self):
        if not hasattr(self, '_updater'):
            from .software_updates import UpdateRuntime
            self._updater = UpdateRuntime(self.paths)
        return self._updater

    def software_update_status(self):
        return self._result(self._software_updater().status, _update_control=True)

    def download_software_update(self):
        return self._result(self._software_updater().download, _update_control=True)

    def apply_software_update(self):
        return self._result(self._software_updater().apply, _update_control=True)

    def cancel_queued_software_update(self):
        return self._result(self._software_updater().cancel_queued_apply, _update_control=True)

    def set_automatic_software_update(self, enabled):
        return self._result(self._software_updater().set_automatic, enabled, _update_control=True)

    def open_software_release(self) -> dict[str, Any]:
        def open_release():
            value = getattr(self, '_offered_software_release', {})
            if value.get('status') != 'available' or not value.get('asset_url'):
                raise SettingsError('请先检查是否有适合这台电脑的新版本')
            return {'opened': bool(webbrowser.open(value['asset_url']))}
        return self._result(open_release)

    def check_team_updates(self) -> dict[str, Any]:
        return self._result(self.lab.check_team_updates)

    def open_method_transfer(self, direction: str) -> dict[str, Any]:
        return self._result(self.lab.open_method_transfer, direction)

    def transfer_methods(self, operation_id: str, keys: list[str]) -> dict[str, Any]:
        return self._result(self.lab.transfer_methods, operation_id, keys)

    def install_team_version(
        self, branch: str, author: str, version_id: str
    ) -> dict[str, Any]:
        return self._result(
            self.lab.install_team_version,
            branch=branch, author=author, version_id=version_id,
        )

    def get_skill_document(
        self, version_id: str, author: str, skill_name: str
    ) -> dict[str, Any]:
        return self._result(
            self.lab.skill_document,
            version_id=version_id, author=author, skill_name=skill_name,
        )

    def save_skill_draft(
        self, skill_name: str, content: str, draft_id: str
    ) -> dict[str, Any]:
        return self._result(
            self.lab.save_skill_draft,
            skill_name=skill_name, content=content, draft_id=draft_id,
        )

    def save_skill_package_draft(
        self, skill_name: str, method: str, foundations: str,
        agent_profile: dict[str, Any], draft_id: str,
    ) -> dict[str, Any]:
        return self._result(
            self.lab.save_skill_package_draft,
            skill_name=skill_name,
            method=method,
            foundations=foundations,
            agent_profile=agent_profile,
            draft_id=draft_id,
        )

    def get_decision_document(
        self, version_id: str, author: str
    ) -> dict[str, Any]:
        return self._result(
            self.lab.decision_document,
            version_id=version_id,
            author=author,
        )

    def save_decision_draft(
        self, script: str, cases: str, draft_id: str
    ) -> dict[str, Any]:
        return self._result(
            self.lab.save_decision_draft,
            script=script,
            cases=cases,
            draft_id=draft_id,
        )

    def test_decision(self, script: str, cases: str) -> dict[str, Any]:
        return self._result(
            self.lab.test_decision,
            script=script,
            cases=cases,
        )

    def upload_skill_draft(self, draft_id: str, description: str) -> dict[str, Any]:
        return self._result(
            self.lab.upload_skill_draft,
            draft_id=draft_id, description=description,
        )

    def estimate_shadow_batch(
        self, selections: list[dict[str, str]], model_profile_id: str = ""
    ) -> dict[str, Any]:
        return self._result(
            self.lab.estimate_batch,
            selections,
            model_profile_id=model_profile_id,
        )

    def start_shadow_batch(
        self, selections: list[dict[str, str]], model_profile_id: str = ""
    ) -> dict[str, Any]:
        return self._result(
            self.lab.start_batch,
            selections=selections, model_profile_id=model_profile_id,
        )

    def get_shadow_batch(self, batch_id: str) -> dict[str, Any]:
        return self._result(self.lab.batch_detail, batch_id)

    def resume_shadow_batch(self, batch_id: str) -> dict[str, Any]:
        return self._result(self.lab.resume_batch, batch_id)

    def save_model_execution_policy(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._result(self.lab.settings.save_execution_policy, value)

    def compare_lab_methods(
        self, left: dict[str, str], right: dict[str, str]
    ) -> dict[str, Any]:
        return self._result(self.lab.compare_methods, left, right)

    def compare_research_runs(self, left: dict[str, str], right: dict[str, str]) -> dict[str, Any]:
        def compare():
            from .run_comparison import compare_runs
            batches = {str(row['batch_id']): None for row in (left, right)}
            for identifier in batches:
                batches[identifier] = self.lab.batch_detail(identifier)
            return compare_runs(batches[left['batch_id']], left['variant_key'],
                                batches[right['batch_id']], right['variant_key'])
        return self._result(compare)

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
        from .operator_control import request_today
        return self._result(request_today, self.paths)

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


def isolated_smoke_paths(root: Path):
    from dataclasses import fields, replace
    original = app_paths()
    return replace(original, **{field.name: root / field.name for field in fields(original)
                                if field.name != 'package_root'})


class GuiRequestLifetime:
    """Quiesce disposable GUI RPCs before removing their owned data directory."""
    def __init__(self):
        from threading import Condition
        self._condition = Condition()
        self._closed = False
        self._active = 0

    def wrap(self, action):
        from functools import wraps
        from inspect import signature
        from types import MethodType
        @wraps(action)
        def request(_bridge, *args, **kwargs):
            with self._condition:
                if self._closed:
                    return {'ok': False, 'error': '窗口已关闭', 'error_type': 'WindowClosed'}
                self._active += 1
            try:
                return action(*args, **kwargs)
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
        # pywebview discovers bound methods and reads their positional names.
        request.__signature__ = signature(action.__func__)
        return MethodType(request, action.__self__)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.wait_for(lambda: self._active == 0)


def desktop_api(bridge, *, lifetime=None):
    from types import SimpleNamespace
    return SimpleNamespace(**{name: lifetime.wrap(getattr(bridge, name)) if lifetime else getattr(bridge, name)
                              for name in dir(type(bridge))
                              if not name.startswith('_') and callable(getattr(bridge, name))})


def _bind_gui_smoke_fixture(bridge, fixture_state, fixture_detail):
    from types import MethodType
    attempts = 0
    completed_transfers = set()

    def fixture_state_api(self):
        return {"ok": True, "value": fixture_state}

    def fixture_resume_api(self, batch_id):
        if batch_id != 'fixture-original-batch':
            return {'ok': False, 'error': 'fixture requires original frozen batch'}
        fixture_state['fixture_resumed_batch'] = batch_id
        return {'ok': True, 'value': {'message': 'fixture original batch resumed'}}

    def fixture_batch_api(self, batch_id):
        return {"ok": True, "value": fixture_detail}

    def fixture_refresh_api(self):
        fixture_detail["labels"]["labels"]["MSFT"] = {
            "status": "final", "official_unadjusted_open": 200.0,
            "official_unadjusted_close": 202.0, "actual_direction": "bullish",
            "confirmed_by_independent_reobservation": True,
            "last_checked_at_et": "2026-09-10T09:00:00-04:00",
        }
        fixture_state["result_refresh"] = {
            "status": "complete", "completed_at": "2026-09-10T09:00:00-04:00",
            "failure_count": 0,
        }
        return {"ok": True, "value": fixture_state["result_refresh"]}

    def fixture_compare_api(self, left, right):
        from .run_comparison import compare_runs
        return self._result(compare_runs, fixture_detail, left['variant_key'],
                            fixture_detail, right['variant_key'])

    def fixture_model_api(self, profile, secret, test=True):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {'ok': False, 'error': 'fixture 401: 登录已失效'}
        return {'ok': True, 'value': {'status': 'connected'}}

    def fixture_update_api(self):
        return {'ok': True, 'value': {'status': 'no_release', 'platform': 'GUI fixture',
                                     'current_version': 'fixture', 'mode': 'installer_only'}}

    def fixture_transfer_api(self, direction):
        return {'ok': True, 'value': {'operation_id': 'fixture-'+direction,
            'destination': 'fixture/repo · versions/shadow_versions',
            'note': 'Fixture only; no remote writes', 'rows': [
                {'key': 'existing', 'author': 'fixture', 'version_id': 'existing',
                 'method_name': '已有测试方法', 'eligible': False,
                 'local_status': '本地已有相同内容', 'content_sha256': 'a'*64},
                {'key': 'new', 'author': 'fixture', 'version_id': 'new',
                 'method_name': '待传输测试方法', 'eligible': direction not in completed_transfers,
                 'local_status': '可选择', 'content_sha256': 'b'*64}]}}

    def fixture_transfer_write_api(self, operation_id, keys):
        if operation_id not in {'fixture-download', 'fixture-upload'} or keys != ['new']:
            return {'ok': False, 'error': 'fixture forbids remote writes'}
        # Synthetic completion only. No real registry or GitHub client is used.
        completed_transfers.add(operation_id.removeprefix('fixture-'))
        return {'ok': True, 'value': [{'key': 'new', 'status': 'complete',
            'message': 'fixture transfer complete; no remote writes'}]}

    bridge.get_lab_state = MethodType(fixture_state_api, bridge)
    bridge.resume_shadow_batch = MethodType(fixture_resume_api, bridge)
    bridge.get_shadow_batch = MethodType(fixture_batch_api, bridge)
    bridge.refresh_prices_and_results = MethodType(fixture_refresh_api, bridge)
    bridge.compare_research_runs = MethodType(fixture_compare_api, bridge)
    bridge.save_lab_model_profile = MethodType(fixture_model_api, bridge)
    bridge.check_software_update = MethodType(fixture_update_api, bridge)
    bridge.open_method_transfer = MethodType(fixture_transfer_api, bridge)
    bridge.transfer_methods = MethodType(fixture_transfer_write_api, bridge)


def launch_desktop(*, smoke_output: Path | None = None) -> int:
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise SettingsError("桌面组件尚未安装，请安装 desktop 依赖") from exc
    temporary = tempfile.TemporaryDirectory(prefix='shaq-native-gui-') if smoke_output else None
    bridge = DesktopBridge(isolated_smoke_paths(Path(temporary.name)) if temporary else None)
    if temporary:
        # Keep real GUI-ready confirmation for shared installed-update fixtures;
        # only disposable windows suppress persistent automatic check workers.
        bridge._software_updater().start_automatic_checks = lambda: None
        from .lab_smoke import run_lab_smoke
        fixture = run_lab_smoke(
            package_root=bridge.paths.package_root,
            output_root=Path(temporary.name) / "fixture",
        )
        fixture_state = fixture["browser_state"]
        fixture_detail = fixture["batch_detail"]
        second_candidate = {
            **fixture_detail["evidence"]["candidates"][0],
            "symbol": "MSFT",
            "selection_method": "fixture_nondefault_candidate",
        }
        fixture_detail["evidence"]["candidates"].append(second_candidate)
        for variant in fixture_detail["variants"].values():
            variant["candidate_intake"]["candidates"].append(dict(second_candidate))
        fixture_state["result_refresh"] = {"status": "idle"}
        fixture_state['jobs'] = [{
            'job_id': 'fixture-recovery', 'batch_id': 'fixture-original-batch',
            'status': 'partial_failure', 'started_at_et': '2026-09-10T08:00:00-04:00',
            'variant_progress': {'team/main': 'failed'}, 'message': 'fixture unfinished call',
            'research_progress': [{
                'variant_key': 'team/main', 'stage': 'tasks_planned',
                'symbols': ['AAPL', 'MSFT'], 'tasks': [
                    {'task_id': 'report:AAPL:price_volume', 'symbol': 'AAPL', 'domain': 'price_volume'},
                    {'task_id': 'report:MSFT:price_volume', 'symbol': 'MSFT', 'domain': 'price_volume'},
                    {'task_id': 'decision'},
                ],
            }, {'variant_key': 'team/main', 'symbol': 'MSFT', 'domain': 'price_volume',
                'stage': 'report_validated', 'status': 'validated',
                'report': {'thesis': '示例已保存报告；不是实际市场分析。',
                           'antithesis': '示例反方说明。', 'unknowns': [], 'invalidation': []},
            }] + [{'variant_key': 'team/main', 'symbol': symbol,
                'domain': 'price_volume', 'stage': 'failure', 'status': 'failed',
                'occurred_at_et': '2026-09-10T08:01:00-04:00'} for symbol in ('AAPL', 'MSFT')],
        }]
        _bind_gui_smoke_fixture(bridge, fixture_state, fixture_detail)
    page = Path(__file__).with_name("desktop") / "index.html"
    if not page.is_file():
        raise FileNotFoundError("desktop interface asset is missing")
    lifetime = GuiRequestLifetime() if temporary else None
    window = webview.create_window(
        "SHAQ Daily Oracle Lab",
        page.as_uri(),
        js_api=desktop_api(bridge, lifetime=lifetime),
        width=1320,
        height=860,
        min_size=(980, 680),
        background_color="#f4f7fb",
    )
    bridge.window = window
    result = {'status': 'failed', 'pages': [], 'model_calls': 'not tested'}
    def inspect_window():
        import time
        try:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if window.evaluate_js("Boolean(window.pywebview && document.querySelector('#run').textContent.trim())"):
                    break
                time.sleep(0.2)
            for page_name in ('run', 'editor', 'history'):
                window.evaluate_js(f"document.querySelector('.nav[data-page={page_name}]').click()")
                if not window.evaluate_js(f"Boolean(document.querySelector('#{page_name}.active').textContent.trim())"):
                    raise RuntimeError(f'Native page failed to render: {page_name}')
                result['pages'].append(page_name)
            window.evaluate_js("document.querySelector('#history tr[data-batch]').click()")
            replay_deadline = time.monotonic() + 5
            while time.monotonic() < replay_deadline:
                if window.evaluate_js("Boolean(document.querySelector('#replay-modal').open && document.querySelector('#candidate-analysis')?.textContent.includes('AAPL'))"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Fixture replay modal or candidate failed to load')
            result['fixture_replay_loaded'] = True
            window.evaluate_js("document.querySelector('.candidate-button[data-symbol=MSFT]').click()")
            candidate_deadline = time.monotonic() + 5
            while time.monotonic() < candidate_deadline:
                selected = window.evaluate_js(
                    "document.querySelector('.candidate-button.active')?.dataset.symbol || ''")
                loaded = window.evaluate_js(
                    "Boolean(document.querySelector('#candidate-analysis')?.textContent.includes('MSFT'))")
                if selected == 'MSFT' and loaded:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Non-default fixture candidate failed to load')
            result['fixture_candidate_selected'] = selected
            window.evaluate_js("document.querySelector('#refresh-button').click()")
            refresh_deadline = time.monotonic() + 5
            while time.monotonic() < refresh_deadline:
                if window.evaluate_js(
                    "document.querySelector('#refresh-status').textContent.includes('完成') && "
                    "document.querySelector('.candidate-button.active')?.dataset.symbol === 'MSFT' && "
                    "document.querySelector('#candidate-analysis')?.textContent.includes('$200.00')"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Fixture refresh did not render updated replay detail')
            result['refresh_completed'] = True
            result['refresh_detail_marker'] = True
            result['refresh_preserved_modal'] = bool(window.evaluate_js(
                "document.querySelector('#replay-modal').open"))
            result['refresh_preserved_candidate'] = bool(window.evaluate_js(
                "document.querySelector('.candidate-button.active')?.dataset.symbol === 'MSFT' && "
                    "document.querySelector('#candidate-analysis')?.textContent.includes('MSFT')"))
            window.evaluate_js("document.querySelector('#replay-close').click()")
            closed = not window.evaluate_js("document.querySelector('#replay-modal').open")
            window.evaluate_js("document.querySelector('#history tr[data-batch]').click()")
            reopen_deadline = time.monotonic() + 5
            while time.monotonic() < reopen_deadline:
                reopened = bool(window.evaluate_js(
                    "document.querySelector('#replay-modal').open && "
                    "document.querySelector('#candidate-analysis')?.textContent.includes('AAPL')"))
                if reopened:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Reopened fixture replay content failed to load')
            result['modal_close_reopen'] = bool(closed and reopened)
            window.evaluate_js("document.querySelector('#replay-close').click(); "
                               "[...document.querySelectorAll('.compare-record')].slice(0,2).forEach(x=>x.click()); "
                               "document.querySelector('#compare-selected').click()")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if window.evaluate_js("document.querySelector('#comparison-modal').open && "
                                      "document.querySelector('#comparison-detail').textContent.includes('模型配置')"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Native frozen-run comparison failed')
            result['comparison_dialog'] = True
            window.evaluate_js("document.querySelector('#comparison-modal').close(); "
                               "document.querySelector('#connections-button').click(); "
                               "document.querySelector('#connect-claude').click()")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if window.evaluate_js("document.querySelector('#model-status').textContent.includes('fixture 401') && "
                                      "!document.querySelector('#model-error-actions').classList.contains('hidden')"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Native connection failure actions missing')
            window.evaluate_js("document.querySelector('#retry-model').click()")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if window.evaluate_js("document.querySelector('#model-status').dataset.status === 'connected'"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Native connection retry failed')
            result['connection_retry_fixture'] = True
            window.evaluate_js("document.querySelector('#close-setup').click(); "
                               "document.querySelector('#software-update-button').click()")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if window.evaluate_js("document.querySelector('#software-update-modal').open && "
                                      "document.querySelector('#software-update-detail').textContent.includes('还没有适合')"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Native software update view failed')
            result['installer_update_view'] = True
            # Exercise real DOM refresh, not just the rendered text. No model,
            # scheduler registration or updater action is invoked by these checks.
            result['update_refresh_preserves_disclosure'] = bool(window.evaluate_js("""
                (()=>{
                    document.querySelector('#software-update-detail details').open=true;
                    renderSoftwareUpdate({status:'current',mode:'installer_only',current_version:'fixture',
                        automatic_enabled:false,notes:'fixture refreshed notes'});
                    return document.querySelector('#software-update-detail details').open;
                })()
            """))
            window.evaluate_js("""
                document.querySelector('#software-update-modal').close();
                document.querySelector('.nav[data-page=run]').click();
                window.scheduleRefreshCheck=null;
                (async()=>{
                    await renderAutomatic();
                    document.querySelector('#edit-automatic').click();
                    const input=document.querySelector('#auto-time');
                    input.value='08:12';input.focus();
                    const model=document.querySelector('#auto-model').value;
                    const version=document.querySelector('.auto-version');
                    version.checked=!version.checked;const chosen=version.checked;
                    await load(false);await renderAutomatic();
                    window.scheduleRefreshCheck=Boolean(
                        document.querySelector('#auto-time')===input && input.value==='08:12' &&
                        document.activeElement===input && document.querySelector('.auto-version').checked===chosen &&
                        document.querySelector('#auto-model').value===model &&
                        !document.querySelector('#automatic-panel').classList.contains('hidden'));
                })().catch(error=>window.scheduleRefreshCheck=String(error));
            """)
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                checked=window.evaluate_js('window.scheduleRefreshCheck')
                if checked is not None:
                    break
                time.sleep(.1)
            result['automatic_refresh_preserves_edit']=checked is True
            if not result['update_refresh_preserves_disclosure'] or not result['automatic_refresh_preserves_edit']:
                raise RuntimeError('Refresh discarded update disclosure or automatic-run form state')
            window.evaluate_js("document.querySelector('#software-update-modal').close(); "
                               "document.querySelector('.nav[data-page=editor]').click()")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if window.evaluate_js("Boolean(document.querySelector('#skill-method').value)"):
                    break
                time.sleep(.1)
            # Let all method loading finish before deliberately dirtying the input.
            window.evaluate_js("window.fixtureEditorNode=document.querySelector('#skill-method'); "
                "window.fixtureEditorVersion=document.querySelector('#edit-version').value; "
                "fixtureEditorNode.value='fixture unsaved method edit'; "
                "fixtureEditorNode.dispatchEvent(new Event('input',{bubbles:true})); "
                "document.querySelector('#download-methods').click()")
            for direction in ('download', 'upload'):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if window.evaluate_js("document.querySelector('#method-transfer-modal').open && "
                                          "document.querySelector('#transfer-select-all') && "
                                          "!document.querySelector('#transfer-select-all').disabled"):
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('Native transfer dialog failed to load')
                if not window.evaluate_js("document.querySelector('#transfer-confirm').disabled"):
                    raise RuntimeError('Native transfer confirmation must start disabled')
                window.evaluate_js("document.querySelector('#transfer-select-all').click(); load(false)")
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if window.evaluate_js("Boolean(document.querySelector('.transfer-check[data-key=new]')?.checked)"):
                        break
                    time.sleep(.1)
                if not window.evaluate_js("fixtureEditorNode===document.querySelector('#skill-method') && "
                    "fixtureEditorNode.value==='fixture unsaved method edit' && "
                    "document.querySelector('#edit-version').value===fixtureEditorVersion && "
                    "document.querySelector('.transfer-check[data-key=existing]').disabled && "
                    "document.querySelector('.transfer-check[data-key=new]').checked && "
                    "!document.querySelector('#transfer-confirm').disabled"):
                    raise RuntimeError('Native transfer lost editor or selection state')
                window.evaluate_js("document.querySelector('#transfer-confirm').click()")
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if window.evaluate_js("document.querySelector('#transfer-confirm').disabled && "
                        "!document.querySelector('.transfer-check[data-key=new]').checked && "
                        "document.querySelector('.transfer-check[data-key=new]').disabled && "
                        "document.querySelector('#method-transfer-detail').textContent.includes('fixture transfer complete')"):
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('Native completed transfer must disable confirmation and selection')
                if not window.evaluate_js("fixtureEditorNode===document.querySelector('#skill-method') && "
                    "fixtureEditorNode.value==='fixture unsaved method edit'"):
                    raise RuntimeError('Native transfer completion lost unsaved editor text')
                window.evaluate_js("document.querySelector('#method-transfer-close').click()")
                if direction == 'download':
                    window.evaluate_js("document.querySelector('#upload-methods').click()")
            result['transfer_preserved_dirty_editor'] = True
            result['transfer_button_states'] = True
            if not all(result[key] for key in (
                'refresh_completed', 'refresh_detail_marker',
                'refresh_preserved_modal', 'refresh_preserved_candidate',
                'modal_close_reopen',
            )):
                raise RuntimeError('Fixture replay state was not preserved through refresh')
            result['status'] = 'passed'
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            smoke_output.write_text(json.dumps(result), encoding='utf-8')
            window.destroy()
    try:
        from .update_gui import GuiSession
        with GuiSession(gate_for(bridge.paths).root, window, admission=bridge._runtime_admission) as session:
            bridge._software_updater().gui_session = session
            webview.start(inspect_window if smoke_output else None, debug=False, private_mode=True)
    finally:
        if temporary:
            # pywebview dispatches RPCs on independent threads. Stop admissions
            # and drain those short fixture requests before deleting their root.
            # Ordinary window closure never waits for or kills analysis workers.
            lifetime.close()
            temporary.cleanup()
    return 0 if not smoke_output or result['status'] == 'passed' else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--research-worker", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-output", type=Path)
    parser.add_argument("--gui-smoke", type=Path)
    args, _ = parser.parse_known_args(argv)
    if getattr(sys, 'frozen', False) and not (args.smoke or args.gui_smoke):
        paths = app_paths()
        gate = gate_for(paths)
        from .app_paths import application_version
        if (gate.root / 'installing.json').exists():
            if args.worker or args.research_worker:
                return 0  # No writes and no recovery window for scheduler launches.
            pending = json.loads((gate.root / 'installing.json').read_text(encoding='utf-8'))
            version = application_version(paths.package_root)
            if pending.get('target_version') == version:
                with gate.target_startup(version):
                    return launch_desktop()
            from .software_updates import launch_update_recovery
            return launch_update_recovery(paths)
    if args.research_worker:
        from .research_schedule import run_research_worker
        return run_research_worker(app_paths().ensure())
    if args.smoke:
        paths = app_paths()
        checks = {
            "desktop_asset": (Path(__file__).with_name("desktop") / "index.html").is_file(),
            "runtime_config": (paths.package_root / "config/runtime.json").is_file(),
            "research_universe": (paths.package_root / "config/research-universe.csv").is_file(),
            "team_repository": (paths.package_root / "config/team-repository.json").is_file(),
            "skills": len(list((paths.package_root / "skills").glob("*/SKILL.md"))) == 8,
            "account_view": (Path(__file__).with_name("desktop") / "accounts.js").is_file(),
            "replay_view": (Path(__file__).with_name("desktop") / "review.js").is_file(),
        }
        smoke: dict[str, Any] = {}
        try:
            from .lab_smoke import run_lab_smoke

            with tempfile.TemporaryDirectory(prefix="shaq-whole-lab-smoke-") as temporary:
                smoke = run_lab_smoke(
                    package_root=paths.package_root, output_root=Path(temporary)
                )
            methods = [
                (row.get("method_name"), row.get("status_badge"))
                for row in smoke.get("methods", [])
            ]
            checks.update({
                "whole_lab_fixture": smoke.get("status") == "passed",
                "zipline_minute_engine": (
                    smoke.get("account_engine") == "zipline-reloaded"
                    and smoke.get("account_engine_version") == "3.1.1"
                ),
                "two_canonical_methods": methods == [
                    ("独立证据门禁版", "正式基准"),
                    ("跨域综合研判版", "Shadow"),
                ],
                "provisional_confirmation": (
                    set(smoke.get("provisional_statuses", [])) == {"provisional"}
                    and set(smoke.get("final_statuses", [])) == {"final"}
                ),
                "frozen_evidence_shared": (
                    len(set(smoke.get("evidence_hashes", {}).values())) == 1
                ),
                "broker_free": (
                    not smoke.get("broker_modules_loaded")
                    and not smoke.get("provider_secrets_used")
                ),
                "reopen_matches": bool(smoke.get("reopen_matches")),
                "accounting_contract": (
                    bool(smoke.get("contract_checks"))
                    and all(smoke["contract_checks"].values())
                ),
                "view_states": {
                    row.get("name"): row.get("status")
                    for row in smoke.get("view_cases", [])
                } == {
                    "long": "final", "empty": "empty", "pending": "pending",
                    "incomplete": "incomplete", "failed": "error",
                },
            })
        except Exception as exc:
            checks["whole_lab_fixture"] = False
            smoke["error"] = str(exc)
        import tables
        checks['no_lzo_runtime'] = tables.which_lib_version('lzo') is None
        serialized = json.dumps({
            **smoke,
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
        }, ensure_ascii=False)
        if args.smoke_output:
            args.smoke_output.write_text(serialized, encoding='utf-8')
        if sys.stdout is not None:
            # Redirected Windows streams may use cp1252, while windowed builds
            # may have no console. The UTF-8 report above remains authoritative.
            try:
                print(json.dumps(json.loads(serialized), ensure_ascii=True))
            except OSError:
                pass  # A closed diagnostic pipe must not turn a result into a modal crash.
        return 0 if all(checks.values()) else 2
    if args.worker:
        return run_worker(paths=app_paths().ensure(), once=args.once)
    return launch_desktop(smoke_output=args.gui_smoke)


if __name__ == "__main__":
    raise SystemExit(main())
