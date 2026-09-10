from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .app_paths import AppPaths
from .hashing import sha256_payload
from .model_backends import ModelProfile, uses_local_subscription
from .settings import SERVICE_NAME, SettingsError, _atomic_json


GITHUB_TOKEN_NAME = "github-user-token"
GITHUB_REFRESH_TOKEN_NAME = "github-user-refresh-token"
MODEL_SECRET_PREFIX = "model-profile:"
OPENBB_SECRET_NAME = "openbb-rest-api-key"


def _repository_defaults(package_root: Path) -> dict[str, Any]:
    path = package_root / "config/team-repository.json"
    if not path.is_file():
        return {
            "owner": "",
            "repository": "",
            "github_app_client_id": "",
            "branch_prefix": "shadow/",
            "catalog_branch": "versions",
            "skill_package_root": "shadow_versions",
            "token_refresh_leeway_seconds": 300,
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: value.get(key, default)
        for key, default in {
            "owner": "",
            "repository": "",
            "github_app_client_id": "",
            "branch_prefix": "shadow/",
            "catalog_branch": "versions",
            "skill_package_root": "shadow_versions",
            "token_refresh_leeway_seconds": 300,
        }.items()
    }


def default_research_settings(package_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "setup_complete": False,
        "application_mode": "research",
        "github": _repository_defaults(package_root),
        "github_login": "",
        "github_upload_allowed": False,
        "github_token_expires_at": "",
        "github_refresh_expires_at": "",
        "active_model_profile_id": "",
        "model_profiles": [],
        "credential_state": {
            "github_token_saved": False,
            "github_refresh_saved": False,
            "model_secret_saved": {},
            "openbb_secret_saved": False,
        },
        "data_profile": {
            "profile_id": "free-research",
            "universe_file": "config/research-universe.csv",
            "metadata_provider": "financedatabase",
            "market_provider": "yfinance",
            "event_provider": "sec-edgar",
            "openbb_base_url": "",
            "openbb_routes": {
                "instrument_identity": "/api/v1/equity/profile",
                "daily_bars": "/api/v1/equity/price/historical",
                "premarket_quotes": "/api/v1/equity/price/historical",
                "primary_events": "/api/v1/equity/filings",
                "option_surface": "/api/v1/derivatives/options/chains"
            },
            "request_timeout_seconds": 30,
            "batch_size": 80,
            "intraday_interval": "5m",
            "maximum_candidates": 8,
            "maximum_event_characters": 60000,
            "maximum_option_contracts_per_side": 40,
        },
        "sec_identity": "",
        "research_readiness": {
            "status": "not_checked",
            "data_profile_sha256": "",
            "checked_at": "",
        },
        "batch": {
            "maximum_parallel_model_calls": 2,
            "forecast_cutoff_et": "08:50:00",
            "forecast_deadline_et": "09:00:00",
        },
    }


class ResearchSettingsStore:
    """Cross-platform research settings with credentials kept outside JSON files."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        if paths.research_settings_file is None:
            raise SettingsError("research settings path is unavailable")

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore
        except ImportError as exc:
            raise SettingsError("系统凭据管理组件尚未安装") from exc
        return keyring

    def load(self) -> dict[str, Any]:
        defaults = default_research_settings(self.paths.package_root)
        path = self.paths.research_settings_file
        if not path.is_file():
            return defaults
        saved = json.loads(path.read_text(encoding="utf-8"))
        merged = {**defaults, **saved}
        merged["github"] = {**defaults["github"], **saved.get("github", {})}
        merged["data_profile"] = {
            **defaults["data_profile"], **saved.get("data_profile", {})
        }
        merged["batch"] = {**defaults["batch"], **saved.get("batch", {})}
        saved_credential_state = saved.get("credential_state")
        if isinstance(saved_credential_state, dict):
            merged["credential_state"] = {
                **defaults["credential_state"],
                **saved_credential_state,
                "model_secret_saved": {
                    **defaults["credential_state"]["model_secret_saved"],
                    **saved_credential_state.get("model_secret_saved", {}),
                },
            }
        else:
            # Older builds did not persist non-secret presence markers.  Migrate
            # from the saved connection metadata without touching Keychain.
            merged["credential_state"] = {
                "github_token_saved": bool(saved.get("github_login")),
                "github_refresh_saved": bool(saved.get("github_refresh_expires_at")),
                "model_secret_saved": {
                    str(row.get("profile_id", "")): True
                    for row in saved.get("model_profiles", [])
                    if row.get("profile_id")
                },
                "openbb_secret_saved": False,
            }
        readiness = merged.get("research_readiness", {})
        if (
            readiness.get("status") != "ready"
            or readiness.get("data_profile_sha256")
            != sha256_payload(merged["data_profile"])
        ):
            merged["setup_complete"] = False
        else:
            merged["setup_complete"] = self._is_setup_complete(merged)
        return merged

    def _set_secret(self, name: str, value: str) -> None:
        secret = value.strip()
        if not secret:
            raise SettingsError("凭据不能为空")
        self._keyring().set_password(SERVICE_NAME, name, secret)

    def _record_secret_state(
        self, kind: str, *, present: bool, profile_id: str = ""
    ) -> None:
        settings = self.load()
        state = settings.setdefault("credential_state", {})
        if kind == "model":
            profiles = state.setdefault("model_secret_saved", {})
            profiles[profile_id] = present
        else:
            state[kind] = present
        self._save(settings)

    def _get_secret(self, name: str) -> str | None:
        try:
            return self._keyring().get_password(SERVICE_NAME, name)
        except Exception:
            return None

    def _delete_secret(self, name: str) -> None:
        try:
            self._keyring().delete_password(SERVICE_NAME, name)
        except Exception:
            pass

    def set_model_secret(self, profile_id: str, value: str) -> None:
        self._set_secret(MODEL_SECRET_PREFIX + profile_id, value)
        self._record_secret_state("model", present=True, profile_id=profile_id)

    def get_model_secret(self, profile_id: str) -> str | None:
        environment_name = "SHAQ_MODEL_API_KEY_" + "".join(
            character if character.isalnum() else "_" for character in profile_id.upper()
        )
        from_environment = os.environ.get(environment_name, "").strip()
        return from_environment or self._get_secret(MODEL_SECRET_PREFIX + profile_id)

    def set_github_token(self, value: str) -> None:
        self._set_secret(GITHUB_TOKEN_NAME, value)
        self._record_secret_state("github_token_saved", present=True)

    def get_github_token(self) -> str | None:
        return os.environ.get("SHAQ_GITHUB_TOKEN", "").strip() or self._get_secret(
            GITHUB_TOKEN_NAME
        )

    def set_github_refresh_token(self, value: str) -> None:
        self._set_secret(GITHUB_REFRESH_TOKEN_NAME, value)
        self._record_secret_state("github_refresh_saved", present=True)

    def get_github_refresh_token(self) -> str | None:
        return self._get_secret(GITHUB_REFRESH_TOKEN_NAME)

    def set_openbb_secret(self, value: str) -> None:
        self._set_secret(OPENBB_SECRET_NAME, value)
        self._record_secret_state("openbb_secret_saved", present=True)

    def get_openbb_secret(self) -> str | None:
        return os.environ.get("SHAQ_OPENBB_API_KEY", "").strip() or self._get_secret(
            OPENBB_SECRET_NAME
        )

    def save_model_profile(
        self, profile_value: dict[str, Any], *, secret: str | None = None
    ) -> dict[str, Any]:
        profile = ModelProfile.from_dict(profile_value)
        if secret:
            self.set_model_secret(profile.profile_id, secret)
        if not uses_local_subscription(profile) and not self.get_model_secret(profile.profile_id):
            raise SettingsError("请填写该模型配置的API密钥")
        settings = self.load()
        profiles = [
            row for row in settings["model_profiles"]
            if row.get("profile_id") != profile.profile_id
        ]
        profiles.append(profile.public_dict())
        settings["model_profiles"] = sorted(profiles, key=lambda row: row["profile_id"])
        settings["active_model_profile_id"] = profile.profile_id
        settings["setup_complete"] = self._is_setup_complete(settings)
        self._save(settings)
        return profile.public_dict()

    def _is_setup_complete(self, settings: dict[str, Any]) -> bool:
        readiness = settings.get("research_readiness", {})
        active_profile = str(settings.get("active_model_profile_id", ""))
        readiness_matches = (
            readiness.get("status") == "ready"
            and readiness.get("data_profile_sha256")
            == sha256_payload(settings.get("data_profile", {}))
        )
        return bool(
            settings.get("model_profiles")
            and active_profile
            and self._model_profile_ready(settings, active_profile)
            and settings.get("sec_identity")
            and settings.get("github_login")
            and self._github_credentials_available(settings)
            and readiness_matches
        )

    def _model_profile_ready(self, settings: dict[str, Any], profile_id: str) -> bool:
        try:
            row = next(
                candidate for candidate in settings.get("model_profiles", [])
                if candidate.get("profile_id") == profile_id
            )
            profile = ModelProfile.from_dict(row)
        except (SettingsError, StopIteration):
            return False
        saved = settings.get("credential_state", {}).get("model_secret_saved", {})
        environment_name = "SHAQ_MODEL_API_KEY_" + "".join(
            character if character.isalnum() else "_"
            for character in profile_id.upper()
        )
        return (
            uses_local_subscription(profile)
            or bool(os.environ.get(environment_name, "").strip())
            or saved.get(profile_id) is True
        )

    def _github_credentials_available(self, settings: dict[str, Any]) -> bool:
        if os.environ.get("SHAQ_GITHUB_TOKEN", "").strip():
            return True
        state = settings.get("credential_state", {})
        if state.get("github_token_saved") is not True:
            return False
        expiry_text = str(settings.get("github_token_expires_at", "")).strip()
        if not expiry_text:
            return True
        try:
            expiry = datetime.fromisoformat(expiry_text)
        except ValueError:
            return False
        if expiry.tzinfo is None:
            return False
        now = datetime.now(timezone.utc)
        if expiry > now:
            return True
        if state.get("github_refresh_saved") is not True:
            return False
        refresh_expiry_text = str(
            settings.get("github_refresh_expires_at", "")
        ).strip()
        if not refresh_expiry_text:
            return False
        try:
            refresh_expiry = datetime.fromisoformat(refresh_expiry_text)
        except ValueError:
            return False
        return refresh_expiry.tzinfo is not None and refresh_expiry > now

    def model_profile(self, profile_id: str | None = None) -> ModelProfile:
        settings = self.load()
        selected = profile_id or str(settings.get("active_model_profile_id", ""))
        for row in settings.get("model_profiles", []):
            if row.get("profile_id") == selected:
                return ModelProfile.from_dict(row)
        raise SettingsError("请选择有效的模型配置")

    def save_research_setup(self, submitted: dict[str, Any]) -> dict[str, Any]:
        settings = self.load()
        allowed = {"sec_identity", "github", "data_profile", "batch"}
        unexpected = set(submitted) - allowed
        if unexpected:
            raise SettingsError("研究设置包含未知字段：" + ", ".join(sorted(unexpected)))
        for key in allowed:
            if key in submitted:
                if isinstance(settings.get(key), dict):
                    settings[key] = {**settings[key], **submitted[key]}
                else:
                    settings[key] = submitted[key]
        if "data_profile" in submitted:
            settings["research_readiness"] = {
                "status": "not_checked",
                "data_profile_sha256": "",
                "checked_at": "",
            }
        github = settings["github"]
        if not str(github.get("owner", "")).strip() or not str(
            github.get("repository", "")
        ).strip():
            raise SettingsError("团队GitHub仓库配置不完整")
        if not str(github.get("branch_prefix", "")).endswith("/"):
            raise SettingsError("GitHub个人分支前缀必须以斜杠结尾")
        data = settings["data_profile"]
        if int(data.get("batch_size", 0)) <= 0 or int(
            data.get("maximum_candidates", 0)
        ) <= 0:
            raise SettingsError("数据批量大小和候选数量必须为正数")
        settings["setup_complete"] = self._is_setup_complete(settings)
        self._save(settings)
        return settings

    def save_research_readiness(self, receipt: dict[str, Any]) -> dict[str, Any]:
        settings = self.load()
        required = {
            "status", "data_profile_sha256", "checked_at", "universe_members",
            "free_bytes", "storage_writable",
        }
        if set(receipt) != required or receipt.get("status") != "ready":
            raise SettingsError("研究环境检查结果无效")
        try:
            checked_at = datetime.fromisoformat(str(receipt.get("checked_at", "")))
            universe_members = int(receipt.get("universe_members", 0))
            free_bytes = int(receipt.get("free_bytes", 0))
        except (TypeError, ValueError) as exc:
            raise SettingsError("研究环境检查结果无效") from exc
        if checked_at.tzinfo is None or universe_members <= 0:
            raise SettingsError("研究环境检查结果无效")
        if receipt.get("data_profile_sha256") != sha256_payload(
            settings.get("data_profile", {})
        ):
            raise SettingsError("数据配置已变化，请重新检查研究环境")
        if receipt.get("storage_writable") is not True or free_bytes <= 0:
            raise SettingsError("本地研究存储不可用")
        settings["research_readiness"] = dict(receipt)
        settings["setup_complete"] = self._is_setup_complete(settings)
        self._save(settings)
        return settings

    def save_github_session(
        self,
        *,
        login: str,
        token: str,
        upload_allowed: bool = False,
        refresh_token: str | None = None,
        expires_in: int | None = None,
        refresh_token_expires_in: int | None = None,
    ) -> dict[str, Any]:
        normalized = login.strip()
        if not normalized or any(character in normalized for character in "/\\\r\n"):
            raise SettingsError("GitHub用户名无效")
        self.set_github_token(token)
        if refresh_token:
            self.set_github_refresh_token(refresh_token)
        elif refresh_token is not None:
            self._delete_secret(GITHUB_REFRESH_TOKEN_NAME)
            self._record_secret_state("github_refresh_saved", present=False)
        settings = self.load()
        settings["github_login"] = normalized
        settings["github_upload_allowed"] = upload_allowed is True
        now = datetime.now(timezone.utc)
        settings["github_token_expires_at"] = self._expiry_time(
            now=now, seconds=expires_in, field="GitHub访问令牌"
        )
        settings["github_refresh_expires_at"] = self._expiry_time(
            now=now,
            seconds=refresh_token_expires_in,
            field="GitHub刷新令牌",
        )
        settings["setup_complete"] = self._is_setup_complete(settings)
        self._save(settings)
        return settings

    @staticmethod
    def _expiry_time(
        *, now: datetime, seconds: int | None, field: str
    ) -> str:
        if seconds is None:
            return ""
        try:
            duration = int(seconds)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{field}有效期无效") from exc
        if duration <= 0:
            raise SettingsError(f"{field}有效期无效")
        return (now + timedelta(seconds=duration)).isoformat()

    def public_settings(self) -> dict[str, Any]:
        settings = self.load()
        state = settings.get("credential_state", {})
        model_state = state.get("model_secret_saved", {})
        return {
            **settings,
            "github_token_saved": (
                bool(os.environ.get("SHAQ_GITHUB_TOKEN", "").strip())
                or state.get("github_token_saved") is True
            ),
            "github_refresh_saved": state.get("github_refresh_saved") is True,
            "model_secret_saved": {
                row["profile_id"]: (
                    uses_local_subscription(ModelProfile.from_dict(row))
                    or bool(os.environ.get(
                        "SHAQ_MODEL_API_KEY_" + "".join(
                            character if character.isalnum() else "_"
                            for character in row["profile_id"].upper()
                        ),
                        "",
                    ).strip())
                    or model_state.get(row["profile_id"]) is True
                )
                for row in settings.get("model_profiles", [])
            },
            "openbb_secret_saved": (
                bool(os.environ.get("SHAQ_OPENBB_API_KEY", "").strip())
                or state.get("openbb_secret_saved") is True
            ),
        }

    def _save(self, settings: dict[str, Any]) -> None:
        _atomic_json(self.paths.research_settings_file, settings)
