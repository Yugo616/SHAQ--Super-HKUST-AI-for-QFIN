from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import shutil
import tempfile
import threading
import difflib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .app_paths import AppPaths
from .data_providers import DataProfile, load_versioned_universe
from .hashing import sha256_payload
from .model_backends import ModelProfile, probe_model_profile
from .research_batch import (
    ResearchBatchRunner,
    VariantSelection,
    load_frozen_evidence,
)
from .research_collection import collect_research_evidence
from .research_dashboard import ResearchDashboardIndex
from .research_labels import refresh_research_labels
from .research_settings import ResearchSettingsStore
from .settings import _atomic_json
from .skill_versions import (
    GitHubRepositoryConfig,
    GitHubSkillClient,
    LocalSkillRegistry,
    SkillVersionManifest,
)


class LabServiceError(ValueError):
    """The desktop research workbench cannot safely complete the requested action."""


ET = ZoneInfo("America/New_York")


SKILL_EXPLANATIONS = {
    "daily-oracle": "总控：让六个领域看同一份冻结证据，并由程序而不是票数决定是否进入0–3只结果。",
    "market-common-shock": "市场：区分大盘、利率、美元、信用与波动冲击，以及这些影响是否可能延续到日内。",
    "pit-peer-spillover": "公司关系：根据行业、客户、供应商、竞争者和互补关系判断消息怎样传导。",
    "primary-event-reasoner": "公司事件：只从截止前一手材料拆解新事实、原有预期和盘前吸收程度。",
    "capital-order-flow": "资金：只有逐笔主动方向与盘口承接语义齐全时才判断短期买卖压力。",
    "derivatives-evidence": "期权：先解释隐含波动范围、偏斜和期限，不把Put、Call或OI机械当方向。",
    "price-volume-structure": "价量：从价格路径、残差缺口、参与度和流动性判断延续或反转。",
    "thesis-adversary": "反方：检查重复证据、遗漏反例、周期错配和完整性问题，但不投票。",
}


class LabService:
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
        self.dashboard = ResearchDashboardIndex(
            batches_root=paths.batches_root, database=paths.research_database
        )
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()

    def state(self) -> dict[str, Any]:
        settings = self.settings.public_settings()
        storage = shutil.disk_usage(self.paths.research_root)
        drafts = (
            self.registry.list_drafts(author=str(settings["github_login"]))
            if settings.get("github_login") else []
        )
        return {
            "product_name": "SHAQ Daily Oracle Lab",
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
            "versions": self.registry.list_versions(),
            "drafts": drafts,
            "skill_explanations": SKILL_EXPLANATIONS,
            "storage": {
                "free_bytes": storage.free,
                "total_bytes": storage.total,
            },
            "dashboard": self.dashboard.overview(),
            "jobs": self.job_statuses(),
        }

    def save_model_profile(
        self, profile: dict[str, Any], *, secret: str, probe: bool = True
    ) -> dict[str, Any]:
        parsed = ModelProfile.from_dict(profile)
        if probe:
            probe_model_profile(profile=parsed, secret=secret)
        return self.settings.save_model_profile(profile, secret=secret)

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
        branch = (
            authorized.ensure_personal_branch(user["login"])
            if access["write"] else None
        )
        self.settings.save_github_session(
            login=user["login"], token=result["token"],
            upload_allowed=access["write"],
            refresh_token=str(result.get("refresh_token", "")),
            expires_in=result.get("expires_in"),
            refresh_token_expires_in=result.get("refresh_token_expires_in"),
        )
        return {
            "status": "authorized", "user": user, "personal_branch": branch,
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
        path = f"skills/{skill_name}/SKILL.md"
        baseline = self.registry.effective_skills("main", "team")[path]
        content = documents[path]
        diff_lines = list(difflib.unified_diff(
            baseline.splitlines(),
            content.splitlines(),
            fromfile=f"main/{skill_name}/SKILL.md",
            tofile=f"{author}/{version_id}/{skill_name}/SKILL.md",
            lineterm="",
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
        }

    def save_skill_draft(
        self, *, skill_name: str, content: str, draft_id: str
    ) -> dict[str, Any]:
        settings = self.settings.load()
        author = str(settings.get("github_login", "")).strip()
        if not author:
            raise LabServiceError("请先登录GitHub再保存个人Shadow草稿")
        client = self._github_client()
        main_sha = client.ref_sha("main")
        if not main_sha:
            raise LabServiceError("团队仓库main版本不可用")
        path = f"skills/{skill_name}/SKILL.md"
        root = self.registry.save_draft(
            author=author, draft_id=draft_id, files={path: content},
            base_main_sha=main_sha,
        )
        return {"draft_id": draft_id, "skill_name": skill_name, "saved": True, "path": root.name}

    def upload_skill_draft(self, *, draft_id: str, description: str) -> dict[str, Any]:
        settings = self.settings.load()
        author = str(settings.get("github_login", "")).strip()
        if not author:
            raise LabServiceError("请先登录GitHub")
        if not settings.get("github_upload_allowed"):
            raise LabServiceError("该GitHub账号只有读取权限，不能上传Shadow版本")
        metadata, files = self.registry.load_draft(author=author, draft_id=draft_id)
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
                content = documents[f"skills/{skill_name}/SKILL.md"]
                unique_domain_documents.add((skill_name, hashlib.sha256(
                    content.encode("utf-8")
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
            key = (str(selection.get("author", "team")), str(selection.get("version_id", "")))
            if key not in available:
                raise LabServiceError(f"Skill version is not installed: {key[0]}/{key[1]}")
            variants.append(VariantSelection.from_registry_row(available[key]))
        return variants

    def start_batch(
        self, *, selections: list[dict[str, str]], model_profile_id: str = ""
    ) -> dict[str, Any]:
        variants = self._resolve_variants(selections)
        profile = self.settings.model_profile(model_profile_id or None)
        secret = self.settings.get_model_secret(profile.profile_id)
        if not secret:
            raise LabServiceError("selected model credential is unavailable")
        job_identity = sha256_payload({
            "date": datetime.now(ET).date().isoformat(),
            "variants": sorted(variant.identity() for variant in variants),
            "profile": profile.identity(),
        })[:16]
        job_id = f"job-{job_identity}"
        with self.jobs_lock:
            existing = self.jobs.get(job_id)
            if existing and existing.get("status") in {"queued", "running"}:
                return dict(existing)
            self.jobs[job_id] = {
                "job_id": job_id, "status": "queued", "started_at_et": None,
                "completed_at_et": None, "message": "等待开始",
            }
        thread = threading.Thread(
            target=self._run_batch_job,
            kwargs={
                "job_id": job_id, "variants": variants,
                "profile": profile, "secret": secret,
            },
            daemon=True,
            name=f"shaq-research-{job_identity}",
        )
        thread.start()
        return dict(self.jobs[job_id])

    def _run_batch_job(
        self, *, job_id: str, variants: list[VariantSelection],
        profile: ModelProfile, secret: str,
    ) -> None:
        self._set_job(job_id, status="running", started_at_et=datetime.now(ET).isoformat(), message="正在冻结共享证据")
        try:
            settings = self.settings.load()
            evidence = self._today_evidence(
                profile=DataProfile.from_dict(settings["data_profile"]),
                sec_identity=str(settings["sec_identity"]),
                openbb_api_key=self.settings.get_openbb_secret() or "",
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
                evidence=evidence, variants=variants, profile=profile, secret=secret
            )
            label_refresh = refresh_research_labels(
                research_root=self.paths.research_root,
                batches_root=self.paths.batches_root,
                profile=DataProfile.from_dict(settings["data_profile"]),
                openbb_api_key=self.settings.get_openbb_secret() or "",
            )
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

    def _today_evidence(
        self, *, profile: DataProfile, sec_identity: str, openbb_api_key: str = ""
    ):
        date_text = datetime.now(ET).date().isoformat()
        locator_root = self.paths.research_root / "evidence_sessions"
        locator_root.mkdir(parents=True, exist_ok=True)
        locator = locator_root / f"{date_text}-{profile.identity()[:12]}.json"
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
        return sorted(stored.values(), key=lambda row: str(row.get("started_at_et") or ""), reverse=True)

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        return self.dashboard.batch_detail(batch_id)

    def export_professor_report(self) -> dict[str, str]:
        destination = self.paths.data_root / "exports/SHAQ_Daily_Oracle_Lab_教授报告.html"
        return {"file": str(self.dashboard.export_professor_report(destination))}
