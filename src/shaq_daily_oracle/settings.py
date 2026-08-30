from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .app_paths import AppPaths


class SettingsError(ValueError):
    """Desktop settings or protected credentials are incomplete."""


SERVICE_NAME = "SHAQ Daily Oracle"
OPENAI_KEY_NAME = "openai-api-key"


def default_settings() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "setup_complete": False,
        "ai_backend": "openai-responses",
        "model": "gpt-5.6",
        "sec_identity": "",
        "opend_host": "127.0.0.1",
        "opend_port": 11111,
        "universe_file": "",
        "automatic_run_enabled": False,
        "automatic_start_et": "08:15:00",
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SettingsStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def load(self) -> dict[str, Any]:
        if not self.paths.settings_file.is_file():
            return default_settings()
        saved = json.loads(self.paths.settings_file.read_text(encoding="utf-8"))
        return {**default_settings(), **saved}

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore
        except ImportError as exc:
            raise SettingsError("系统凭据管理组件尚未安装") from exc
        return keyring

    def set_openai_key(self, value: str) -> None:
        secret = value.strip()
        if not secret:
            raise SettingsError("OpenAI API Key 不能为空")
        self._keyring().set_password(SERVICE_NAME, OPENAI_KEY_NAME, secret)

    def get_openai_key(self) -> str | None:
        from_environment = os.environ.get("OPENAI_API_KEY", "").strip()
        if from_environment:
            return from_environment
        try:
            return self._keyring().get_password(SERVICE_NAME, OPENAI_KEY_NAME)
        except Exception:
            return None

    def save_setup(self, submitted: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        allowed = {
            "ai_backend", "model", "sec_identity", "opend_host", "opend_port",
            "universe_file", "automatic_start_et",
        }
        unexpected = set(submitted) - allowed - {"openai_api_key"}
        if unexpected:
            raise SettingsError(f"设置包含未知字段：{', '.join(sorted(unexpected))}")
        value = {**current, **{key: submitted[key] for key in allowed if key in submitted}}
        if value["ai_backend"] not in {"openai-responses", "codex-cli"}:
            raise SettingsError("AI 后端只能选择 OpenAI API 或 Codex 开发模式")
        if not str(value["model"]).strip():
            raise SettingsError("模型名称不能为空")
        if not str(value["sec_identity"]).strip():
            raise SettingsError("SEC 研究身份不能为空")
        if not str(value["universe_file"]).strip() or not Path(
            str(value["universe_file"])
        ).expanduser().is_file():
            raise SettingsError("请选择有效的股票池文件")
        port = int(value["opend_port"])
        if not 1 <= port <= 65535:
            raise SettingsError("OpenD 端口无效")
        value["opend_port"] = port
        if value["ai_backend"] == "openai-responses":
            supplied_key = str(submitted.get("openai_api_key", "")).strip()
            if supplied_key:
                self.set_openai_key(supplied_key)
            if not self.get_openai_key():
                raise SettingsError("请填写 OpenAI API Key")
        value["setup_complete"] = True
        value.pop("openai_api_key", None)
        _atomic_json(self.paths.settings_file, value)
        self.write_effective_ai_config(value)
        return value

    def write_effective_ai_config(self, settings: dict[str, Any] | None = None) -> Path:
        value = settings or self.load()
        base = json.loads(
            (self.paths.package_root / "config/ai-backend.json").read_text(encoding="utf-8")
        )
        base["backend"] = value["ai_backend"]
        base["model"] = value["model"]
        _atomic_json(self.paths.effective_ai_config, base)
        return self.paths.effective_ai_config

    def apply_to_environment(self) -> dict[str, str]:
        value = self.load()
        if value.get("setup_complete") is not True:
            raise SettingsError("请先完成首次设置")
        environment = {
            "DAILY_ORACLE_SEC_USER_AGENT": str(value["sec_identity"]),
            "DAILY_ORACLE_UNIVERSE": str(Path(value["universe_file"]).expanduser().resolve()),
            "DAILY_ORACLE_AI_CONFIG": str(self.write_effective_ai_config(value)),
            "FUTU_OPEND_HOST": str(value["opend_host"]),
            "FUTU_OPEND_PORT": str(value["opend_port"]),
        }
        if value["ai_backend"] == "openai-responses":
            key = self.get_openai_key()
            if not key:
                raise SettingsError("系统凭据管理器中没有 OpenAI API Key")
            environment["OPENAI_API_KEY"] = key
        os.environ.update(environment)
        return environment
