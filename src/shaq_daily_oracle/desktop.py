from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import webbrowser
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


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class DesktopBridge:
    def __init__(self, paths=None) -> None:
        self.paths = (paths or app_paths()).ensure()
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

    @staticmethod
    def _result(action, *args, **kwargs) -> dict[str, Any]:
        try:
            return {"ok": True, "value": action(*args, **kwargs)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

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

    def check_team_updates(self) -> dict[str, Any]:
        return self._result(self.lab.check_team_updates)

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

    def compare_lab_methods(
        self, left: dict[str, str], right: dict[str, str]
    ) -> dict[str, Any]:
        return self._result(self.lab.compare_methods, left, right)

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


def desktop_api(bridge):
    from types import SimpleNamespace
    return SimpleNamespace(**{name: getattr(bridge, name) for name in dir(type(bridge))
                              if not name.startswith('_') and callable(getattr(bridge, name))})


def launch_desktop(*, smoke_output: Path | None = None) -> int:
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise SettingsError("桌面组件尚未安装，请安装 desktop 依赖") from exc
    temporary = tempfile.TemporaryDirectory(prefix='shaq-native-gui-') if smoke_output else None
    bridge = DesktopBridge(isolated_smoke_paths(Path(temporary.name)) if temporary else None)
    if temporary:
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
        bridge.get_lab_state = lambda: {"ok": True, "value": fixture_state}
        bridge.get_shadow_batch = lambda batch_id: {
            "ok": True, "value": fixture_detail}
        def fixture_refresh():
            fixture_state["result_refresh"] = {
                "status": "complete", "completed_at": "2026-09-10T09:00:00-04:00",
                "failure_count": 0,
            }
            return {"ok": True, "value": fixture_state["result_refresh"]}
        bridge.refresh_prices_and_results = fixture_refresh
    page = Path(__file__).with_name("desktop") / "index.html"
    if not page.is_file():
        raise FileNotFoundError("desktop interface asset is missing")
    window = webview.create_window(
        "SHAQ Daily Oracle Lab",
        page.as_uri(),
        js_api=desktop_api(bridge),
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
                if window.evaluate_js("Boolean(document.querySelector('#replay-modal').open && document.querySelector('#candidate-analysis').textContent.includes('AAPL'))"):
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
                    "document.querySelector('#candidate-analysis').textContent.includes('MSFT')")
                if selected == 'MSFT' and loaded:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Non-default fixture candidate failed to load')
            result['fixture_candidate_selected'] = selected
            window.evaluate_js("document.querySelector('#refresh-button').click()")
            refresh_deadline = time.monotonic() + 5
            while time.monotonic() < refresh_deadline:
                if window.evaluate_js("document.querySelector('#refresh-status').textContent.includes('完成')"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Fixture refresh did not complete')
            result['refresh_completed'] = True
            result['refresh_preserved_modal'] = bool(window.evaluate_js(
                "document.querySelector('#replay-modal').open"))
            result['refresh_preserved_candidate'] = bool(window.evaluate_js(
                "document.querySelector('.candidate-button.active')?.dataset.symbol === 'MSFT' && "
                "document.querySelector('#candidate-analysis').textContent.includes('MSFT')"))
            window.evaluate_js("document.querySelector('#replay-close').click()")
            closed = not window.evaluate_js("document.querySelector('#replay-modal').open")
            window.evaluate_js("document.querySelector('#history tr[data-batch]').click()")
            reopen_deadline = time.monotonic() + 5
            while time.monotonic() < reopen_deadline:
                reopened = bool(window.evaluate_js(
                    "document.querySelector('#replay-modal').open && "
                    "document.querySelector('#candidate-analysis').textContent.includes('AAPL')"))
                if reopened:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Reopened fixture replay content failed to load')
            result['modal_close_reopen'] = bool(closed and reopened)
            if not all(result[key] for key in (
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
        webview.start(inspect_window if smoke_output else None, debug=False, private_mode=True)
    finally:
        if temporary:
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
