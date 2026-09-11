from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import csv
import shutil
import tempfile
import threading
import uuid
import difflib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from filelock import FileLock, Timeout as LockTimeout

from .app_paths import AppPaths
from .data_providers import DataProfile, load_versioned_universe
from .hashing import sha256_payload
from .model_backends import ModelProfile, probe_model_profile, uses_local_subscription
from .research_batch import (
    ResearchBatchRunner,
    VariantSelection,
    load_frozen_evidence,
)
from .research_collection import collect_research_evidence
from .research_dashboard import ResearchDashboardIndex
from .research_labels import refresh_research_labels
from .research_progress import ResearchProgressLog
from .research_settings import ResearchSettingsStore
from .settings import _atomic_json
from .skill_versions import (
    GitHubRepositoryConfig,
    GitHubSkillClient,
    LocalSkillRegistry,
    SkillVersionManifest,
    parse_agent_profile,
    render_agent_profile,
    validate_artifact_set,
)


class LabServiceError(ValueError):
    """The desktop research workbench cannot safely complete the requested action."""


ET = ZoneInfo("America/New_York")
RESULT_REFRESH_INTERVAL = timedelta(minutes=15)


SKILL_EXPLANATIONS = {
    "daily-oracle": "每日研究总控 · Daily Oracle Workflow：让六个领域看同一份冻结证据，并由程序而不是票数决定是否进入0–3只结果。",
    "market-common-shock": "市场环境 · Market Context：区分大盘、利率、美元、信用与波动冲击，以及这些影响是否可能延续到日内。",
    "pit-peer-spillover": "行业与关系传导 · Industry & Relationship Spillovers：根据行业、客户、供应商、竞争者和互补关系判断消息怎样传导。",
    "primary-event-reasoner": "公司催化事件 · Company Catalysts：只从截止前一手材料拆解新事实、原有预期和盘前吸收程度。",
    "capital-order-flow": "买卖压力与流动性 · Order Flow & Liquidity：只有逐笔主动方向与盘口承接语义齐全时才判断短期买卖压力。",
    "derivatives-evidence": "期权定价与仓位线索 · Options Pricing & Positioning：先解释隐含波动范围、偏斜和期限，不把Put、Call或OI机械当方向。",
    "price-volume-structure": "价格走势与参与度 · Price Action & Participation：从价格路径、残差缺口、参与度和流动性判断延续或反转。",
    "thesis-adversary": "反方审查 · Adversarial Review：检查重复证据、遗漏反例、周期错配和完整性问题，但不投票。",
}


class LabService:
    def module_document(self, *, module: str, version_id: str, author: str, draft_id: str = ""):
        from .module_rules import MODULES, default_rule
        if module not in MODULES:
            raise LabServiceError("未知计算模块")
        docs = self.registry.effective_skills(version_id, author)
        if draft_id:
            _, edits = self.registry.load_draft(author=self._local_author(), draft_id=draft_id)
            docs.update(edits)
        example = {"candidates": [{"symbol": "EXAMPLE"}], "maximum_candidates": 1} if module == "screening" else {"symbol": "EXAMPLE", "evidence": []}
        expected = {"symbols": ["EXAMPLE"]} if module == "screening" else {}
        return {"script": docs.get(f"modules/{module}/compute.js", default_rule(module)),
                "cases": docs.get(f"modules/{module}/cases.json", json.dumps({"reference": "现有基准方法；新增公式时填写对应研究依据", "cases": [{"name": "基本输入", "input": example, "expected": expected}]}, ensure_ascii=False, indent=2))}

    def save_module_draft(self, *, module, script, cases, draft_id):
        from .module_rules import MODULES, test_rule
        if module not in MODULES:
            raise LabServiceError("未知计算模块")
        receipt = test_rule(script, cases)
        self.registry.save_draft(author=self._local_author(), draft_id=draft_id,
            base_main_sha=self.registry.main_version()["version_sha256"],
            files={f"modules/{module}/compute.js": script, f"modules/{module}/cases.json": cases})
        return receipt

    def get_local_draft(self, draft_id):
        _, docs = self.registry.load_draft(author=self._local_author(), draft_id=draft_id)
        return docs

    def _local_author(self) -> str:
        return str(self.settings.load().get("github_login") or "local").lower()

    def copy_local_version(self, *, version_id: str, author: str) -> dict[str, Any]:
        files = self.registry.effective_skills(version_id, author)
        draft_id = "edit-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.registry.save_draft(
            author=self._local_author(), draft_id=draft_id, files=files,
            base_main_sha=self.registry.main_version()["version_sha256"],
        )
        return {"draft_id": draft_id, "base_version": version_id}

    def finalize_local_version(self, *, draft_id: str, description: str) -> dict[str, Any]:
        author = self._local_author()
        metadata, files = self.registry.load_draft(author=author, draft_id=draft_id)
        # Store the complete package so future main edits cannot alter this version.
        complete = {**self.registry.effective_skills("main", "team"), **files}
        manifest = SkillVersionManifest.create(
            version_id=draft_id, author=author, base_main_sha=metadata["base_main_sha"],
            files=complete, description=description.strip() or draft_id,
        )
        self.registry.install(manifest=manifest, files=complete, commit_sha="local:" + manifest.identity())
        return {"version_id": draft_id, "author": author}

    def __init__(self, paths: AppPaths) -> None:
        if not all((
            paths.research_root, paths.batches_root, paths.skill_registry_root,
            paths.research_database, paths.research_settings_file,
        )):
            raise LabServiceError("research application paths are unavailable")
        self.paths = paths
        self.settings = ResearchSettingsStore(paths)
        self.registry = LocalSkillRegistry(
            root=paths.skill_registry_root, package_skills=paths.package_root / "skills"
        )
        from .bundled_versions import install_bundled_versions
        install_bundled_versions(self.registry)
        self.dashboard = ResearchDashboardIndex(
            batches_root=paths.batches_root, database=paths.research_database
        )
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()
        from .virtual_accounts import AccountStore, AccountRules
        account_store = AccountStore(paths.research_root / 'virtual_accounts')
        if not (account_store.root / 'activation.json').exists():
            account_store.activate(AccountRules())

    def _refresh_minute_accounts(self, profile):
        from .minute_settlements import refresh_minute_observations
        from .virtual_accounts import AccountStore
        try:
            with FileLock(str(self.paths.research_root / 'minute_refresh.lock'), timeout=0):
                # A batch completion and reopen refresh can arrive concurrently.
                # The whole collect/reconcile pair is one background operation.
                rows = self.dashboard.overview()['daily_results']
                result = refresh_minute_observations(research_root=self.paths.research_root,
                            rows=rows, profile=profile)
                rows = self.dashboard.account_rows(rows)
                AccountStore(self.paths.research_root / 'virtual_accounts').refresh(rows)
                return result
        except LockTimeout:
            return {'status': 'already_running', 'refreshed_dates': [], 'failures': []}

    @property
    def _result_refresh_receipt(self) -> Path:
        return self.paths.research_root / "label_refresh_status.json"

    def result_refresh_status(self) -> dict[str, Any]:
        try:
            return json.loads(self._result_refresh_receipt.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"status": "idle", "operation_id": "", "result": {}}

    def start_result_refresh(self, *, manual: bool = False) -> dict[str, Any]:
        """Start one credential-free price/result refresh across app instances."""
        now = datetime.now(ET)
        prior = self.result_refresh_status()
        lock = FileLock(
            str(self.paths.research_root / "result_refresh.lock"),
            timeout=0, thread_local=False,
        )
        try:
            lock.acquire()
        except LockTimeout:
            current = self.result_refresh_status()
            return {**current, "status": "already_running"}
        if not manual and prior.get("attempted_at"):
            try:
                attempted = datetime.fromisoformat(str(prior["attempted_at"])).astimezone(ET)
            except ValueError:
                attempted = None
            if attempted is not None and now - attempted < RESULT_REFRESH_INTERVAL:
                lock.release()
                return {**prior, "status": "not_due", "next_eligible_at": (
                    attempted + RESULT_REFRESH_INTERVAL).isoformat()}

        operation_id = uuid.uuid4().hex
        running = {
            "status": "running", "operation_id": operation_id,
            "attempted_at": now.isoformat(), "manual": manual,
        }
        _atomic_json(self._result_refresh_receipt, running)
        threading.Thread(
            target=self._run_result_refresh,
            args=(lock, running), daemon=True,
            name="shaq-result-refresh",
        ).start()
        return running

    def _run_result_refresh(self, lock: FileLock, running: dict[str, Any]) -> None:
        try:
            profile = DataProfile.from_dict(self.settings.load()["data_profile"])
            if profile.market_provider != "yfinance":
                raise LabServiceError(
                    "价格与成绩刷新仅使用无需凭据的 Yahoo 数据配置；当前配置未执行"
                )
            result = refresh_research_labels(
                research_root=self.paths.research_root,
                batches_root=self.paths.batches_root,
                profile=profile,
            )
            result["minute_settlement"] = self._refresh_minute_accounts(profile)
            failures = list(result.get("failures", []))
            minute = result.get("minute_settlement", {})
            failures.extend(minute.get("failures", []))
            status = "partial_failure" if failures else "complete"
            _atomic_json(self._result_refresh_receipt, {
                **running, "status": status, "completed_at": datetime.now(ET).isoformat(),
                "result": result, "failure_count": len(failures),
            })
        except Exception as exc:
            _atomic_json(self._result_refresh_receipt, {
                **running, "status": "failed", "completed_at": datetime.now(ET).isoformat(),
                "error_type": type(exc).__name__, "error": str(exc),
            })
        finally:
            lock.release()

    def refresh_labels_if_due(self):
        """Compatibility entry point for the research scheduler."""
        return self.start_result_refresh(manual=False)

    def state(self) -> dict[str, Any]:
        self.start_result_refresh(manual=False)
        from .market_calendar import market_session, next_market_session
        now = datetime.now(ET)
        session = market_session(now.date())
        settings = self.settings.public_settings()
        storage = shutil.disk_usage(self.paths.research_root)
        drafts = self.registry.list_drafts(author=str(settings.get("github_login") or "local"))
        return {
            "product_name": "SHAQ Daily Oracle Lab",
            "clock": {"et": now.isoformat(), "trade_date": now.date().isoformat(),
                      "is_trading_day": session is not None,
                      "next_trade_date": (session or next_market_session(now.date())).session_date.isoformat()},
            "platform": platform.system(),
            "research_mode": {
                "available": True, "orders_allowed": False,
                "broker_modules_loaded": False,
            },
            "operator_mode": {
                "platform_supported": platform.system() == "Darwin",
                "safety_ready": False,
                "requires_separate_setup": True,
            },
            "settings": settings,
            "versions": self.history_linked_versions(),
            "drafts": drafts,
            "skill_explanations": SKILL_EXPLANATIONS,
            "storage": {
                "free_bytes": storage.free,
                "total_bytes": storage.total,
            },
            "dashboard": self.dashboard.overview(),
            "data_status": self.data_status(),
            "jobs": self.job_statuses(),
            "result_refresh": self.result_refresh_status(),
        }

    def compare_methods(
        self, left: dict[str, str], right: dict[str, str]
    ) -> dict[str, Any]:
        """Return an installed-method comparison without executing either method."""

        def canonical(selection: dict[str, str]) -> dict[str, Any]:
            author = str(selection.get("author", "team")).lower()
            version_id = str(selection.get("version_id", ""))
            for row in self.registry.selectable_versions():
                if str(row.get("author", "team")).lower() != author:
                    continue
                if version_id == str(row["version_id"]) or version_id in {
                    str(alias) for alias in row.get("aliases", [])
                }:
                    return row
            raise LabServiceError(
                f"Skill version is not installed: {author}/{version_id}"
            )

        left_row, right_row = canonical(left), canonical(right)
        if left_row.get("author") != right_row.get("author"):
            raise LabServiceError("method comparison requires one installed author")
        comparison = self.registry.compare_methods(
            str(left_row["version_id"]), str(right_row["version_id"]),
            author=str(left_row["author"]),
        )
        identity_fields = ("author", "version_id", "method_name", "status_badge")
        return {
            **comparison,
            "left_method": {key: left_row.get(key) for key in identity_fields},
            "right_method": {key: right_row.get(key) for key in identity_fields},
        }

    def data_status(self) -> dict[str, Any]:
        settings = self.settings.load()
        profile = DataProfile.from_dict(settings["data_profile"])
        universe_path = Path(profile.universe_file)
        if not universe_path.is_absolute():
            universe_path = self.paths.package_root / universe_path
        universe_rows: list[dict[str, str]] = []
        if universe_path.is_file():
            with universe_path.open(newline="", encoding="utf-8-sig") as handle:
                universe_rows = list(csv.DictReader(handle))
        universe_observed = max(
            (str(row.get("observed_active_at_utc", "")) for row in universe_rows),
            default="",
        )
        evidence_manifest = None
        locators = sorted(
            (self.paths.research_root / "evidence_sessions").glob("*.json"),
            reverse=True,
        )
        if locators:
            try:
                locator = json.loads(locators[0].read_text(encoding="utf-8"))
                manifest_path = (
                    self.paths.research_root / "evidence" /
                    locator["evidence_hash"] / "evidence_manifest.json"
                )
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                unsigned = {
                    key: item for key, item in value.items() if key != "evidence_hash"
                }
                if value.get("evidence_hash") != sha256_payload(unsigned):
                    raise LabServiceError("latest evidence manifest hash mismatch")
                evidence_manifest = value
            except Exception:
                evidence_manifest = None
        captured_at = str(evidence_manifest.get("as_of_et", "")) if evidence_manifest else ""
        collection = (
            evidence_manifest.get("provider_manifest", {}).get("collection_statuses", [])
            if evidence_manifest else []
        )
        status_counts: dict[str, int] = {}
        for row in collection:
            status = str(row.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
        run_status = "fresh" if evidence_manifest else "not_run"
        run_note = (
            "本次批跑开始时重新联网采集，并冻结给所有版本共用。"
            if evidence_manifest else "尚未生成本机证据；开始今日批跑时会重新联网采集。"
        )
        rows = [
            {
                "name": "股票、市场与行业行情",
                "source": profile.market_provider,
                "updated_at": captured_at,
                "status": run_status,
                "coverage": "全股票池筛选；候选保存完整价格路径与盘前状态" if evidence_manifest else "将在运行时检查",
                "note": run_note,
            },
            {
                "name": "公司一手公告",
                "source": profile.event_provider,
                "updated_at": captured_at,
                "status": run_status,
                "coverage": "截止时间前SEC公告；当天没有公告会标为今日无事件",
                "note": run_note,
            },
            {
                "name": "基础期权表面",
                "source": profile.market_provider,
                "updated_at": captured_at,
                "status": "limited" if evidence_manifest else "not_run",
                "coverage": "候选的到期日、隐含波动、偏斜与期限结构",
                "note": "免费源不提供可靠的主动买卖与开平仓语义，因此只能解释波动结构。",
            },
            {
                "name": "证券身份与行业资料",
                "source": profile.metadata_provider,
                "updated_at": captured_at,
                "status": run_status,
                "coverage": "候选公司身份与行业；不凭相关性虚构客户供应商关系",
                "note": run_note,
            },
            {
                "name": "标普500研究股票池",
                "source": universe_rows[0].get("source_role", "unknown") if universe_rows else "unavailable",
                "updated_at": universe_observed,
                "status": "versioned" if universe_rows else "provider_error",
                "coverage": f"{len(universe_rows)}只；每行保存已知时间",
                "note": "当前为版本化研究快照；成分变化只从被记录的已知时间起生效。",
            },
            {
                "name": "逐笔主动买卖与盘口深度",
                "source": "免费源未提供",
                "updated_at": "",
                "status": "unavailable",
                "coverage": "0",
                "note": "资金领域不会用聚合大单或成交量猜测主动买卖方向。",
            },
            {
                "name": "期权主动交易与开平仓",
                "source": "免费源未提供",
                "updated_at": "",
                "status": "unavailable",
                "coverage": "0",
                "note": "不会把Call、Put或未平仓量机械映射成涨跌。",
            },
            {
                "name": "客户、供应商与竞争网络",
                "source": "未配置可靠PIT来源",
                "updated_at": "",
                "status": "unavailable",
                "coverage": "行业资料可用，具体经济关系不可用",
                "note": "行业共同变化仍可分析，但不会声称存在未经证明的公司关系。",
            },
        ]
        if evidence_manifest:
            names = {'cboe_vix': 'Cboe波动率历史', 'federal_reserve_h15': '美联储公开利率', 'nasdaq_earnings': 'Nasdaq财报日历与预期'}
            for item in evidence_manifest.get('provider_manifest', {}).get('public_source_statuses', []):
                rows.append({'name': names.get(item['provider'], item['provider']), 'source': item.get('source_uri', ''),
                             'updated_at': item.get('captured_at', ''), 'status': item['status'],
                             'coverage': '', 'note': item.get('error', '仅使用本次已保存的公开资料')})
        return {
            "latest_evidence_hash": evidence_manifest["evidence_hash"] if evidence_manifest else "",
            "latest_cutoff_status": (
                evidence_manifest.get("cutoff_status") if evidence_manifest else "not_run"
            ),
            "collection_status_counts": status_counts,
            "items": rows,
        }

    def save_model_profile(
        self, profile: dict[str, Any], *, secret: str, probe: bool = True
    ) -> dict[str, Any]:
        parsed = ModelProfile.from_dict(profile)
        if probe:
            probe_model_profile(profile=parsed, secret=secret)
        return self.settings.save_model_profile(
            profile, secret=None if uses_local_subscription(parsed) else secret
        )

    def save_setup(self, submitted: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(submitted)
        openbb_secret = str(sanitized.pop("openbb_api_key", "")).strip()
        if openbb_secret:
            self.settings.set_openbb_secret(openbb_secret)
        return self.settings.save_research_setup(sanitized)

    def check_research_environment(self) -> dict[str, Any]:
        settings = self.settings.load()
        profile = DataProfile.from_dict(settings["data_profile"])
        universe_path = Path(profile.universe_file)
        if not universe_path.is_absolute():
            universe_path = self.paths.package_root / universe_path
        if not universe_path.is_file():
            raise LabServiceError("版本化股票池文件不可用")
        members = load_versioned_universe(
            universe_path,
            cutoff=datetime.now(ET),
        )
        if not members:
            raise LabServiceError("当前时点的PIT股票池为空")
        self.paths.research_root.mkdir(parents=True, exist_ok=True)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".storage-check-", dir=self.paths.research_root
        )
        os.close(descriptor)
        Path(probe_name).unlink(missing_ok=True)
        storage = shutil.disk_usage(self.paths.research_root)
        receipt = {
            "status": "ready",
            "data_profile_sha256": profile.identity(),
            "checked_at": datetime.now(ET).isoformat(),
            "universe_members": len(members),
            "free_bytes": storage.free,
            "storage_writable": True,
        }
        return self.settings.save_research_readiness(receipt)

    def _github_client(self, *, require_token: bool = False) -> GitHubSkillClient:
        settings = self.settings.load()
        config = GitHubRepositoryConfig.from_dict(settings["github"])
        token = self.settings.get_github_token() or ""
        environment_token = os.environ.get("SHAQ_GITHUB_TOKEN", "").strip()
        expiry_text = str(settings.get("github_token_expires_at", "")).strip()
        if token and not environment_token and expiry_text:
            try:
                expiry = datetime.fromisoformat(expiry_text)
            except ValueError as exc:
                raise LabServiceError("GitHub登录有效期记录损坏，请重新登录") from exc
            if expiry.tzinfo is None:
                raise LabServiceError("GitHub登录有效期记录损坏，请重新登录")
            refresh_at = datetime.now(timezone.utc) + timedelta(
                seconds=config.token_refresh_leeway_seconds
            )
            if expiry <= refresh_at:
                refresh_token = self.settings.get_github_refresh_token() or ""
                refresh_expiry_text = str(
                    settings.get("github_refresh_expires_at", "")
                ).strip()
                if not refresh_token or not refresh_expiry_text:
                    raise LabServiceError("GitHub登录已过期，请重新登录")
                try:
                    refresh_expiry = datetime.fromisoformat(refresh_expiry_text)
                except ValueError as exc:
                    raise LabServiceError(
                        "GitHub刷新令牌记录损坏，请重新登录"
                    ) from exc
                if (
                    refresh_expiry.tzinfo is None
                    or refresh_expiry <= datetime.now(timezone.utc)
                ):
                    raise LabServiceError("GitHub登录已过期，请重新登录")
                refreshed = GitHubSkillClient(
                    config=config, token=""
                ).refresh_user_token(refresh_token)
                self.settings.save_github_session(
                    login=str(settings.get("github_login", "")),
                    token=refreshed["token"],
                    upload_allowed=settings.get("github_upload_allowed") is True,
                    refresh_token=str(refreshed.get("refresh_token", "")),
                    expires_in=refreshed.get("expires_in"),
                    refresh_token_expires_in=refreshed.get(
                        "refresh_token_expires_in"
                    ),
                )
                token = refreshed["token"]
        if require_token and not token:
            raise LabServiceError("GitHub login is required")
        return GitHubSkillClient(config=config, token=token)

    def begin_github_login(self) -> dict[str, Any]:
        return self._github_client().begin_device_flow()

    def complete_github_login(self, device_code: str) -> dict[str, Any]:
        client = self._github_client()
        result = client.poll_device_flow(device_code)
        if result.get("status") != "authorized":
            return result
        authorized = GitHubSkillClient(config=client.config, token=result["token"])
        user = authorized.authenticated_user()
        access = authorized.repository_access()
        if not access["read"]:
            raise LabServiceError("GitHub账号无法读取团队仓库")
        self.settings.save_github_session(
            login=user["login"], token=result["token"],
            upload_allowed=access["write"],
            refresh_token=str(result.get("refresh_token", "")),
            expires_in=result.get("expires_in"),
            refresh_token_expires_in=result.get("refresh_token_expires_in"),
        )
        return {
            "status": "authorized", "user": user,
            "catalog_branch": authorized.config.catalog_branch,
            "repository_access": access, "upload_allowed": access["write"],
        }

    def check_team_updates(self) -> list[dict[str, Any]]:
        client = self._github_client(require_token=False)
        installed = {
            (row.get("author"), row.get("version_id"), row.get("source_commit_sha"))
            for row in self.registry.list_versions()
        }
        output = []
        for row in client.list_remote_versions():
            output.append({
                **row,
                "installed": (
                    row.get("author"), row.get("version_id"), row.get("commit_sha")
                ) in installed,
            })
        return output

    def install_team_version(self, *, branch: str, author: str, version_id: str) -> dict[str, Any]:
        manifest, files, commit = self._github_client().download_version(
            branch=branch, author=author, version_id=version_id
        )
        self.registry.install(manifest=manifest, files=files, commit_sha=commit)
        return next(
            row for row in self.registry.list_versions()
            if row.get("author") == manifest.author and row.get("version_id") == manifest.version_id
        )

    def skill_document(
        self, *, version_id: str, author: str, skill_name: str
    ) -> dict[str, Any]:
        if skill_name not in SKILL_EXPLANATIONS:
            raise LabServiceError("unknown Skill")
        documents = self.registry.effective_skills(version_id, author)
        baseline_documents = self.registry.effective_skills("main", "team")
        method_path = f"skills/{skill_name}/SKILL.md"
        foundations_path = f"skills/{skill_name}/references/foundations.md"
        agent_path = f"skills/{skill_name}/agents/openai.yaml"
        content = documents[method_path]
        diff_lines = list(difflib.unified_diff(
            baseline_documents[method_path].splitlines(), content.splitlines(),
            fromfile=f"main/{skill_name}/SKILL.md", tofile=f"{author}/{version_id}/{skill_name}/SKILL.md", lineterm="",
        ))
        return {
            "skill_name": skill_name,
            "content": content,
            "chinese_explanation": SKILL_EXPLANATIONS[skill_name],
            "version_id": version_id,
            "author": author,
            "diff_from_main": "\n".join(diff_lines),
            "changed_lines": sum(
                1 for line in diff_lines
                if line.startswith(("+", "-"))
                and not line.startswith(("+++", "---"))
            ),
            "package": {
                "method": content,
                "foundations": documents[foundations_path],
                "agent_profile": parse_agent_profile(documents[agent_path]),
                "paths": {"method": method_path, "foundations": foundations_path, "agent": agent_path},
            },
        }

    def save_skill_draft(
        self, *, skill_name: str, content: str, draft_id: str
    ) -> dict[str, Any]:
        settings = self.settings.load()
        author = self._local_author()
        main_sha = self.registry.main_version()["version_sha256"]
        path = f"skills/{skill_name}/SKILL.md"
        root = self.registry.save_draft(
            author=author, draft_id=draft_id, files={path: content},
            base_main_sha=main_sha,
        )
        return {"draft_id": draft_id, "skill_name": skill_name, "saved": True, "path": root.name}

    def save_skill_package_draft(
        self, *, skill_name: str, method: str, foundations: str,
        agent_profile: dict[str, Any], draft_id: str,
    ) -> dict[str, Any]:
        if skill_name not in SKILL_EXPLANATIONS:
            raise LabServiceError("unknown Skill")
        settings = self.settings.load()
        author = self._local_author()
        main_sha = self.registry.main_version()["version_sha256"]
        baseline = self.registry.effective_skills("main", "team")
        agent_path = f"skills/{skill_name}/agents/openai.yaml"
        baseline_profile = parse_agent_profile(baseline[agent_path])
        if agent_profile.get("allow_implicit_invocation") != baseline_profile["allow_implicit_invocation"]:
            raise LabServiceError("角色卡不能改变系统调用规则")
        files = {
            f"skills/{skill_name}/SKILL.md": method,
            f"skills/{skill_name}/references/foundations.md": foundations,
            agent_path: render_agent_profile(agent_profile),
        }
        root = self.registry.save_draft(
            author=author, draft_id=draft_id, files=files, base_main_sha=main_sha,
        )
        return {"draft_id": draft_id, "skill_name": skill_name, "saved": True, "path": root.name, "files": sorted(files)}

    def decision_document(self, *, version_id: str, author: str) -> dict[str, Any]:
        documents = self.registry.effective_skills(version_id, author)
        baseline = self.registry.effective_skills("main", "team")
        script_path = "decision/decision.js"
        cases_path = "decision/cases.json"
        script = documents[script_path]
        cases = documents[cases_path]
        receipt = validate_artifact_set({script_path: script, cases_path: cases})
        diff_lines = list(difflib.unified_diff(
            baseline[script_path].splitlines(), script.splitlines(),
            fromfile="main/decision/decision.js",
            tofile=f"{author}/{version_id}/decision/decision.js",
            lineterm="",
        ))
        return {
            "version_id": version_id,
            "author": author,
            "script": script,
            "cases": cases,
            "diff_from_main": "\n".join(diff_lines),
            "changed_lines": sum(
                1 for line in diff_lines
                if line.startswith(("+", "-"))
                and not line.startswith(("+++", "---"))
            ),
            "test_receipt": receipt,
        }

    def save_decision_draft(
        self, *, script: str, cases: str, draft_id: str
    ) -> dict[str, Any]:
        settings = self.settings.load()
        author = self._local_author()
        main_sha = self.registry.main_version()["version_sha256"]
        files = {
            "decision/decision.js": script,
            "decision/cases.json": cases,
        }
        receipt = validate_artifact_set(files)
        root = self.registry.save_draft(
            author=author,
            draft_id=draft_id,
            files=files,
            base_main_sha=main_sha,
        )
        return {
            "draft_id": draft_id,
            "saved": True,
            "path": root.name,
            "files": sorted(files),
            "test_receipt": receipt,
        }

    def test_decision(self, *, script: str, cases: str) -> dict[str, Any]:
        receipt = validate_artifact_set({
            "decision/decision.js": script,
            "decision/cases.json": cases,
        })
        return {"test_receipt": receipt}

    def upload_skill_draft(self, *, draft_id: str, description: str) -> dict[str, Any]:
        settings = self.settings.load()
        author = str(settings.get("github_login", "")).strip()
        if not author:
            raise LabServiceError("请先登录GitHub")
        if not settings.get("github_upload_allowed"):
            raise LabServiceError("该GitHub账号只有读取权限，不能上传Shadow版本")
        metadata, files = self.registry.load_draft(author=author, draft_id=draft_id)
        validate_artifact_set(files)
        changed_domains = {
            Path(path).parts[1] for path in files if path.startswith("skills/")
        }
        for domain in changed_domains:
            required = {
                f"skills/{domain}/SKILL.md",
                f"skills/{domain}/references/foundations.md",
                f"skills/{domain}/agents/openai.yaml",
            }
            if not required.issubset(files):
                raise LabServiceError("每个上传领域必须包含分析方法、研究依据和角色卡")
        decision_paths = {"decision/decision.js", "decision/cases.json"}
        if decision_paths.intersection(files) and not decision_paths.issubset(files):
            raise LabServiceError("决策规则与固定测试必须一起上传")
        manifest = SkillVersionManifest.create(
            version_id=draft_id, author=author,
            base_main_sha=metadata["base_main_sha"], files=files,
            description=description,
        )
        return self._github_client(require_token=True).upload_version(
            login=author, manifest=manifest, files=files
        )

    def estimate_batch(
        self, selections: list[dict[str, str]], model_profile_id: str = ""
    ) -> dict[str, Any]:
        variants = self._resolve_variants(selections)
        settings = self.settings.load()
        model_profile = self.settings.model_profile(model_profile_id or None)
        data_profile = DataProfile.from_dict(settings["data_profile"])
        unique_domain_documents = set()
        for variant in variants:
            documents = self.registry.effective_skills(variant.version_id, variant.author)
            for skill_name in SKILL_EXPLANATIONS:
                if skill_name == "daily-oracle":
                    continue
                package = {
                    path: documents[path]
                    for path in (
                        f"skills/{skill_name}/SKILL.md",
                        f"skills/{skill_name}/references/foundations.md",
                        f"skills/{skill_name}/agents/openai.yaml",
                    )
                }
                unique_domain_documents.add((skill_name, hashlib.sha256(
                    json.dumps(package, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()))
        unique_calls = len(unique_domain_documents)
        concurrent_slots = min(model_profile.max_concurrency, max(unique_calls, 1))
        maximum_wait_minutes = math.ceil(
            math.ceil(unique_calls / concurrent_slots) * model_profile.timeout_seconds / 60
        )
        available_domains = ["市场", "公司关系", "公司事件", "价量"]
        limited_domains = ["期权（波动结构可用，主动交易方向不可用）"]
        unavailable_domains = ["资金（缺少逐笔主动方与盘口深度）"]
        if data_profile.metadata_provider == "none":
            available_domains.remove("公司关系")
            limited_domains.append("公司关系（只有PIT行业资料）")
        return {
            "selected_versions": len(variants),
            "maximum_unique_model_calls": unique_calls,
            "maximum_model_wait_minutes": maximum_wait_minutes,
            "available_domains": available_domains,
            "limited_domains": limited_domains,
            "unavailable_domains": unavailable_domains,
            "note": "实际调用会因无数据领域和内容哈希缓存而减少。",
        }

    def _resolve_variants(self, selections: list[dict[str, str]]) -> list[VariantSelection]:
        available = {
            (str(row.get("author", "team")), str(row["version_id"])): row
            for row in self.registry.list_versions()
        }
        variants = []
        for selection in selections:
            key = (str(selection.get("author", "team")).lower(), str(selection.get("version_id", "")))
            if key not in available:
                raise LabServiceError(f"Skill version is not installed: {key[0]}/{key[1]}")
            variants.append(VariantSelection.from_registry_row(available[key]))
        return variants

    def start_batch(
        self, *, selections: list[dict[str, str]], model_profile_id: str = ""
    ) -> dict[str, Any]:
        variants = self._resolve_variants(selections)
        profile = self.settings.model_profile(model_profile_id or None)
        secret = "" if uses_local_subscription(profile) else (self.settings.get_model_secret(profile.profile_id) or "")
        if not secret and not uses_local_subscription(profile):
            raise LabServiceError("selected model credential is unavailable")
        job_identity = sha256_payload({
            "date": datetime.now(ET).date().isoformat(),
            "variants": sorted(variant.identity() for variant in variants),
            "profile": profile.identity(),
        })[:16]
        job_id = f"job-{job_identity}"
        lock_root = self.paths.research_root / 'jobs'
        lock_root.mkdir(parents=True, exist_ok=True)
        task_lock = FileLock(str(lock_root / (job_id + '.lock')), thread_local=False)
        try:
            task_lock.acquire(timeout=0)
        except LockTimeout:
            saved = lock_root / (job_id + '.json')
            return json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {
                'job_id': job_id, 'status': 'running', 'message': '已有任务正在运行',
            }
        with self.jobs_lock:
            existing = self.jobs.get(job_id)
            if existing and existing.get("status") in {"queued", "running"}:
                task_lock.release()
                return dict(existing)
            self.jobs[job_id] = {
                "job_id": job_id, "status": "queued", "started_at_et": None,
                "completed_at_et": None, "message": "等待开始",
                "variant_progress": {f'{v.author}/{v.version_id}': 'queued' for v in variants},
            }
        thread = threading.Thread(
            target=self._run_batch_job,
            kwargs={
                "job_id": job_id, "variants": variants,
                "profile": profile, "secret": secret,
                "task_lock": task_lock,
            },
            daemon=True,
            name=f"shaq-research-{job_identity}",
        )
        thread.start()
        return dict(self.jobs[job_id])

    def _run_batch_job(
        self, *, job_id: str, variants: list[VariantSelection],
        profile: ModelProfile, secret: str, task_lock=None,
    ) -> None:
        self._set_job(job_id, status="running", started_at_et=datetime.now(ET).isoformat(), message="正在冻结共享证据")
        try:
            settings = self.settings.load()
            evidence = self._today_evidence(
                profile=DataProfile.from_dict(settings["data_profile"]),
                sec_identity=str(settings["sec_identity"]),
                variants=variants,
                openbb_api_key=(self.settings.get_openbb_secret() or "") if "openbb-rest" in (
                    settings["data_profile"].get("market_provider"), settings["data_profile"].get("event_provider"),
                    settings["data_profile"].get("metadata_provider"),
                ) else "",
            )
            self._set_job(job_id, message="正在运行所选Skill版本")
            integration = json.loads(
                (self.paths.package_root / "config/integration.json").read_text(encoding="utf-8")
            )
            runner = ResearchBatchRunner(
                batches_root=self.paths.batches_root,
                cache_root=self.paths.research_root / "cache/model_calls",
                registry=self.registry, integration_policy=integration,
            )
            result = runner.run(
                evidence=evidence, variants=variants, profile=profile, secret=secret,
                progress=lambda key, status: self._variant_progress(job_id, key, status),
                observer=ResearchProgressLog(
                    self.paths.research_root / "jobs" / f"{job_id}-research.jsonl"
                ).append,
            )
            try:
                label_refresh = refresh_research_labels(
                    research_root=self.paths.research_root,
                    batches_root=self.paths.batches_root,
                    profile=DataProfile.from_dict(settings["data_profile"]),
                    openbb_api_key=(self.settings.get_openbb_secret() or "") if settings["data_profile"].get("market_provider") == "openbb-rest" else "",
                )
                label_refresh['minute_settlement'] = self._refresh_minute_accounts(
                    DataProfile.from_dict(settings['data_profile']))
            except Exception as exc:
                label_refresh = {"status": "failed", "error": str(exc)}
            completed = result["status"]["all_variants_completed"]
            self._set_job(
                job_id, status="complete" if completed else "partial_failure",
                completed_at_et=datetime.now(ET).isoformat(),
                message="批量Shadow已完成" if completed else "部分版本失败，其他结果已保留",
                batch_id=result["status"]["batch_id"],
                label_refresh=label_refresh,
            )
        except Exception as exc:
            self._set_job(
                job_id, status="failed", completed_at_et=datetime.now(ET).isoformat(),
                message=str(exc), error_type=type(exc).__name__,
            )
        finally:
            if task_lock is not None:
                task_lock.release()

    def _variant_progress(self, job_id, key, status):
        with self.jobs_lock:
            progress = dict(self.jobs[job_id].get('variant_progress', {}))
            progress[key] = status
            self.jobs[job_id]['variant_progress'] = progress
        self._set_job(job_id)

    def _today_evidence(
        self, *, profile: DataProfile, sec_identity: str, openbb_api_key: str = "", variants=None,
    ):
        from .module_rules import default_rule
        screening_rules = {}
        for variant in variants or []:
            docs = self.registry.effective_skills(variant.version_id, variant.author)
            script = docs.get("modules/screening/compute.js", default_rule("screening"))
            screening_rules[sha256_payload(script)] = script
        date_text = datetime.now(ET).date().isoformat()
        locator_root = self.paths.research_root / "evidence_sessions"
        locator_root.mkdir(parents=True, exist_ok=True)
        locator = locator_root / f"{date_text}-{profile.identity()[:12]}-{sha256_payload(screening_rules)[:12]}.json"
        if locator.is_file():
            value = json.loads(locator.read_text(encoding="utf-8"))
            return load_frozen_evidence(
                self.paths.research_root / "evidence" / value["evidence_hash"]
            )
        staging_root = self.paths.research_root / "evidence_staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{date_text}-", dir=staging_root)) / "evidence"
        try:
            evidence = collect_research_evidence(
                root=temporary, package_root=self.paths.package_root,
                profile=profile, sec_identity=sec_identity,
                openbb_api_key=openbb_api_key,
                screening_rules=screening_rules or None,
                history_cache_root=self.paths.research_root / "cache/daily_bars",
                allow_replay=True,
            )
            destination = self.paths.research_root / "evidence" / evidence.manifest["evidence_hash"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                load_frozen_evidence(destination)
                shutil.rmtree(temporary.parent, ignore_errors=True)
            else:
                temporary.replace(destination)
                temporary.parent.rmdir()
            _atomic_json(locator, {
                "schema_version": 1, "trade_date": date_text,
                "data_profile_sha256": profile.identity(),
                "evidence_hash": evidence.manifest["evidence_hash"],
            })
            return load_frozen_evidence(destination)
        except Exception:
            shutil.rmtree(temporary.parent, ignore_errors=True)
            raise

    def _set_job(self, job_id: str, **updates: Any) -> None:
        with self.jobs_lock:
            self.jobs[job_id] = {**self.jobs.get(job_id, {"job_id": job_id}), **updates}
            job = dict(self.jobs[job_id])
        jobs_root = self.paths.research_root / "jobs"
        _atomic_json(jobs_root / f"{job_id}.json", job)

    def history_linked_versions(self):
        from .history_methods import equivalent_method_documents
        from .research_dashboard import _verified_skill_snapshot
        versions = self.registry.selectable_versions()
        documents = [(row, self.registry.effective_skills(row['version_id'], row['author']))
                     for row in versions]
        for path in self.paths.batches_root.glob('*/skills/*.json'):
            try:
                snapshot = _verified_skill_snapshot(path)
                variant = snapshot['variant']
                key = f"{variant['author']}/{variant['version_id']}"
                matches = [row for row, docs in documents
                           if equivalent_method_documents(snapshot['documents'], docs)]
                if len(matches) == 1:
                    aliases = matches[0].setdefault('history_keys', [])
                    if key not in aliases:
                        aliases.append(key)
            except (ValueError, KeyError, OSError):
                continue
        return versions

    def job_statuses(self) -> list[dict[str, Any]]:
        stored = {}
        jobs_root = self.paths.research_root / "jobs"
        if jobs_root.is_dir():
            for path in jobs_root.glob("job-*.json"):
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    stored[str(row["job_id"])] = row
                except Exception:
                    continue
        with self.jobs_lock:
            stored.update({key: dict(value) for key, value in self.jobs.items()})
        for job_id, row in stored.items():
            row["research_progress"] = ResearchProgressLog(
                self.paths.research_root / "jobs" / f"{job_id}-research.jsonl"
            ).read()
        return sorted(stored.values(), key=lambda row: str(row.get("started_at_et") or ""), reverse=True)

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        detail = self.dashboard.batch_detail(batch_id)
        accounts = self.dashboard.overview()['virtual_accounts']
        detail['virtual_accounts'] = dict(accounts, results=[r for r in accounts['results'] if r['batch_id'] == batch_id])
        detail["research_progress"] = [
            event for job in self.job_statuses() if job.get("batch_id") == batch_id
            for event in job.get("research_progress", []) if event.get("batch_id") == batch_id
        ]
        return detail
