from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .hashing import sha256_payload


class ModelBackendError(ValueError):
    """A configured model endpoint cannot produce an auditable structured result."""


SUPPORTED_PROTOCOLS = {
    "openai-responses",
    "openai-chat-completions",
    "anthropic-messages",
    "codex-cli",
    "claude-code",
}
SUPPORTED_OUTPUT_MODES = {"strict", "local_validated"}


@dataclass(frozen=True)
class ProtocolCapabilities:
    """Request behavior that is stable for a protocol, never inferred from model names."""

    provider_kind: str
    local_subscription: bool
    sampling_parameters: tuple[str, ...]
    policy_version: int = 1

    def identity_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["sampling_parameters"] = list(self.sampling_parameters)
        return value


_PROTOCOL_CAPABILITIES = {
    "openai-responses": ProtocolCapabilities(
        provider_kind="openai-official",
        local_subscription=False,
        sampling_parameters=(),
    ),
    "openai-chat-completions": ProtocolCapabilities(
        provider_kind="openai-compatible-relay",
        local_subscription=False,
        sampling_parameters=(),
    ),
    "anthropic-messages": ProtocolCapabilities(
        provider_kind="anthropic-official",
        local_subscription=False,
        sampling_parameters=(),
    ),
    "codex-cli": ProtocolCapabilities(
        provider_kind="codex-cli",
        local_subscription=True,
        sampling_parameters=(),
    ),
    "claude-code": ProtocolCapabilities(
        provider_kind="claude-code",
        local_subscription=True,
        sampling_parameters=(),
    ),
}


def _protocol_capabilities(protocol: str) -> ProtocolCapabilities:
    try:
        return _PROTOCOL_CAPABILITIES[protocol]
    except KeyError as exc:
        raise ModelBackendError(f"unsupported model protocol: {protocol}") from exc


def _effective_protocol_policy(profile: "ModelProfile") -> dict[str, Any]:
    capabilities = _protocol_capabilities(profile.protocol)
    return {
        "protocol_policy_version": capabilities.policy_version,
        "provider_kind": capabilities.provider_kind,
        "sampling_parameters": list(capabilities.sampling_parameters),
    }


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    protocol: str
    base_url: str
    model: str
    auth_style: str = "bearer"
    output_mode: str = "strict"
    timeout_seconds: int = 180
    maximum_output_tokens: int = 12000
    maximum_context_tokens: int = 128000
    max_concurrency: int = 1
    rate_limit_per_minute: int = 30
    reasoning_effort: str = "high"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelProfile":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unexpected = set(value) - allowed
        if unexpected:
            raise ModelBackendError(
                "model profile contains unknown fields: " + ", ".join(sorted(unexpected))
            )
        profile = cls(**value)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not self.profile_id.strip() or not self.model.strip():
            raise ModelBackendError("model profile id and model are required")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ModelBackendError(f"unsupported model protocol: {self.protocol}")
        if self.output_mode not in SUPPORTED_OUTPUT_MODES:
            raise ModelBackendError(f"unsupported output mode: {self.output_mode}")
        if self.protocol not in {"codex-cli", "claude-code"}:
            parsed = urlparse(self.base_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ModelBackendError("remote model base_url must be an absolute HTTPS URL")
        if self.auth_style not in {"bearer", "x-api-key"}:
            raise ModelBackendError("unsupported model authentication style")
        for name in (
            "timeout_seconds",
            "maximum_output_tokens",
            "maximum_context_tokens",
            "max_concurrency",
            "rate_limit_per_minute",
        ):
            if int(getattr(self, name)) <= 0:
                raise ModelBackendError(f"{name} must be positive")
        if self.maximum_output_tokens >= self.maximum_context_tokens:
            raise ModelBackendError("maximum output tokens must be smaller than the context limit")
        for price in (self.input_price_per_million, self.output_price_per_million):
            if price is not None and float(price) < 0:
                raise ModelBackendError("token prices cannot be negative")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> str:
        return sha256_payload(self.public_dict())

    def request_policy_identity(self) -> str:
        """Hash transport behavior separately from the historical model identity."""

        return sha256_payload(
            _protocol_capabilities(self.protocol).identity_payload()
        )

    def endpoint_fingerprint(self) -> str:
        if self.protocol in {"codex-cli", "claude-code"}:
            return hashlib.sha256(self.protocol.encode("utf-8")).hexdigest()
        parsed = urlparse(self.base_url)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_result(value: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelBackendError("model output is not a JSON object")
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance=value, schema=schema)
    except ImportError as exc:
        raise ModelBackendError("jsonschema is required for model output validation") from exc
    except Exception as exc:
        path = ".".join(str(part) for part in getattr(exc, "path", ())) or "root"
        validator = str(getattr(exc, "validator", "schema"))
        raise ModelBackendError(
            f"model output failed JSON Schema validation at {path} ({validator})"
        ) from exc
    return value


def _json_from_text(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ModelBackendError("model returned no text output")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelBackendError("model output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelBackendError("model output must be a JSON object")
    return value


def _endpoint(base_url: str, suffix: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith(suffix):
        return normalized
    if suffix.startswith("/v1/") and normalized.endswith("/v1"):
        return normalized + suffix[3:]
    return normalized + suffix


def _auth_headers(profile: ModelProfile, secret: str) -> dict[str, str]:
    if not secret.strip():
        raise ModelBackendError("model API credential is unavailable")
    if profile.auth_style == "x-api-key":
        return {"x-api-key": secret}
    return {"Authorization": f"Bearer {secret}"}


def uses_local_subscription(profile: ModelProfile) -> bool:
    """Whether the model is authenticated by its own locally logged-in CLI."""

    return _protocol_capabilities(profile.protocol).local_subscription


def find_desktop_cli(executable: str, search_roots: list[Path]) -> str | None:
    """Native windows do not inherit a terminal's augmented PATH."""
    for root in search_roots:
        direct = root / executable
        if direct.is_file() and os.access(direct, os.X_OK):
            return str(direct)
        if executable != 'codex':
            continue
        for bundle in sorted(root.glob('*.app')):
            try:
                metadata = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            candidate = bundle / 'Contents/Resources' / executable
            if metadata.get('CFBundleIdentifier') == 'com.openai.codex' and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _local_cli(profile: ModelProfile) -> str:
    executable = "codex" if profile.protocol == "codex-cli" else "claude"
    if sys.platform == "win32":
        search_roots = [
            Path(value) / suffix
            for value, suffix in (
                (os.environ.get("USERPROFILE", ""), Path(".local/bin")),
                (os.environ.get("APPDATA", ""), Path("npm")),
                (os.environ.get("LOCALAPPDATA", ""), Path("Programs")),
            )
            if value
        ]
        located = find_desktop_cli(f"{executable}.exe", search_roots)
        if not located:
            located = shutil.which(executable)
        if not located:
            located = next(
                (
                    candidate
                    for launcher in (f"{executable}.cmd", f"{executable}.bat")
                    if (candidate := find_desktop_cli(launcher, search_roots))
                ),
                None,
            )
    else:
        located = shutil.which(executable) or find_desktop_cli(executable, [
            Path.home() / '.local/bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin'),
            Path('/Applications'), Path.home() / 'Applications',
        ])
    if not located:
        raise ModelBackendError(
            f"未找到 {executable}。请在此电脑先安装并登录，再回到应用连接。"
        )
    return located


def _local_cli_environment() -> dict[str, str]:
    """Keep only ordinary locale/PATH values; never pass evidence through env vars."""

    allowed = {
        "APPDATA", "COMSPEC", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
        "LOCALAPPDATA", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TERM", "TMP",
        "TMPDIR", "USER", "USERPROFILE", "WINDIR",
    }
    return {
        key: value for key, value in os.environ.items()
        if key.upper() in allowed
    }


def _cli_command(executable: str, arguments: list[str]) -> list[str]:
    """Launch supported Windows shims explicitly while keeping prompts on stdin."""

    if sys.platform != "win32":
        return [executable, *arguments]
    suffix = Path(executable).suffix.lower()
    if suffix in {".exe", ".com"}:
        return [executable, *arguments]
    if suffix in {".cmd", ".bat"}:
        raise ModelBackendError(
            "检测到旧式 Windows CLI 启动器；请安装官方原生 .exe 后重新连接"
        )
    raise ModelBackendError(f"unsupported Windows CLI launcher: {suffix or 'no extension'}")


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
    r"|authorization|x-api-key)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_COMMON_API_TOKEN = re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_-]{8,}\b")


def safe_model_error_summary(
    value: object, *, sensitive_values: tuple[str, ...] = (), maximum_length: int = 800
) -> str:
    """Return a bounded diagnostic with common credentials removed."""

    text = str(value).strip() or "未知错误"
    for sensitive in sensitive_values:
        if sensitive:
            text = text.replace(sensitive, "[REDACTED]")
    text = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _BEARER_VALUE.sub("Bearer [REDACTED]", text)
    text = _COMMON_API_TOKEN.sub("[REDACTED]", text)
    text = " ".join(text.split())
    return text[-maximum_length:]


def _codex_cli_call(
    *, profile: ModelProfile, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the user's existing Codex login; evidence lives only in a temporary workspace."""

    executable = _local_cli(profile)
    with tempfile.TemporaryDirectory(prefix="shaq-codex-packet-") as temporary:
        root = Path(temporary)
        schema_path = root / "output-schema.json"
        output_path = root / "final.json"
        schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
        arguments = [
            "exec", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
            "--output-schema", str(schema_path), "--output-last-message", str(output_path),
            "--color", "never", "-C", str(root), "-",
        ]
        if profile.model != "subscription-default":
            arguments[1:1] = ["--model", profile.model]
        command = _cli_command(executable, arguments)
        try:
            completed = subprocess.run(
                command, input=prompt, text=True, encoding="utf-8", errors="replace",
                capture_output=True, shell=False,
                cwd=root, env=_local_cli_environment(), timeout=profile.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelBackendError("Codex 本地调用超时") from exc
        if completed.returncode != 0 or not output_path.is_file():
            detail = safe_model_error_summary(completed.stderr or completed.stdout)
            raise ModelBackendError(f"Codex 本地调用失败：{detail}")
        parsed = _json_from_text(output_path.read_text(encoding="utf-8"))
    return parsed, {
        "backend": "codex-cli",
        "response_id": "local-subscription",
        "response_model": profile.model,
        "usage": None,
        "request_policy": {
            "ephemeral": True, "sandbox": "read-only", "user_config": False,
            "rules": False, "output_schema": True,
            **_effective_protocol_policy(profile),
        },
        "schema_enforcement": "cli-output-schema",
    }


def _claude_code_call(
    *, profile: ModelProfile, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use Claude Code's existing local subscription, with no agent tools enabled."""

    executable = _local_cli(profile)
    arguments = [
        "-p", "--output-format", "json", "--json-schema",
        json.dumps(schema, sort_keys=True), "--permission-mode", "plan", "--tools", "",
    ]
    if profile.model != "subscription-default":
        arguments.extend(["--model", profile.model])
    command = _cli_command(executable, arguments)
    try:
        completed = subprocess.run(
            command, input=prompt, text=True, encoding="utf-8", errors="replace",
            capture_output=True, shell=False,
            env=_local_cli_environment(), timeout=profile.timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ModelBackendError("Claude 本地调用超时") from exc
    if completed.returncode != 0:
        detail = safe_model_error_summary(completed.stderr or completed.stdout)
        raise ModelBackendError(f"Claude 本地调用失败：{detail}")
    envelope = _json_from_text(completed.stdout)
    parsed = envelope.get("structured_output")
    if not isinstance(parsed, dict):
        raise ModelBackendError("Claude structured_output is missing or is not a JSON object")
    return parsed, {
        "backend": "claude-code",
        "response_id": str(envelope.get("session_id", "local-subscription")),
        "response_model": profile.model,
        "usage": envelope.get("usage") if isinstance(envelope.get("usage"), dict) else None,
        "request_policy": {
            "permission_mode": "plan", "tools": [], "json_schema": True,
            **_effective_protocol_policy(profile),
        },
        "schema_enforcement": "cli-json-schema",
    }


def _model_identity_matches(requested: str, returned: str) -> bool:
    requested_value = requested.strip().lower()
    returned_value = returned.strip().lower()
    return bool(
        requested_value
        and returned_value
        and (
            returned_value == requested_value
            or returned_value.startswith(requested_value + "-")
            or requested_value.startswith(returned_value + "-")
        )
    )


def _openai_responses_call(
    *, profile: ModelProfile, secret: str, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        raise ModelBackendError("OpenAI Python SDK is unavailable") from exc
    client_kwargs: dict[str, Any] = {
        "api_key": secret,
        "timeout": float(profile.timeout_seconds),
        "max_retries": 0,
    }
    if profile.base_url:
        client_kwargs["base_url"] = profile.base_url
    client = OpenAI(**client_kwargs)
    request: dict[str, Any] = {
        "model": profile.model,
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "shaq_daily_oracle_output",
                "strict": True,
                "schema": schema,
            }
        },
        "tools": [],
        "tool_choice": "none",
        "store": False,
        "max_output_tokens": profile.maximum_output_tokens,
    }
    if profile.reasoning_effort:
        request["reasoning"] = {"effort": profile.reasoning_effort}
    response = client.responses.create(**request)
    parsed = _json_from_text(getattr(response, "output_text", None))
    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return parsed, {
        "backend": "openai-responses",
        "response_id": str(getattr(response, "id", "")),
        "response_model": str(getattr(response, "model", profile.model)),
        "usage": usage if isinstance(usage, dict) else None,
        "request_policy": {
            "store": False, "tools": [], "tool_choice": "none",
            **_effective_protocol_policy(profile),
        },
        "schema_enforcement": "native",
    }


def _http_post_json(
    *, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except ImportError as exc:
        raise ModelBackendError("httpx is unavailable") from exc
    sensitive: list[str] = []
    for value in headers.values():
        if not value or value.lower() == "application/json":
            continue
        sensitive.append(value)
        if value.lower().startswith("bearer "):
            sensitive.append(value.split(" ", 1)[1])
    sensitive_values = tuple(sensitive)
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        value = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        category = {
            401: "authentication rejected",
            403: "access forbidden",
            429: "rate limited",
        }.get(status, "HTTP error")
        raise ModelBackendError(
            f"model endpoint request failed: HTTP {status} ({category})"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ModelBackendError("model endpoint request failed: timeout") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelBackendError("model endpoint returned malformed JSON") from exc
    except Exception as exc:
        detail = safe_model_error_summary(exc, sensitive_values=sensitive_values)
        raise ModelBackendError(
            f"model endpoint request failed: {type(exc).__name__}: {detail}"
        ) from exc
    if not isinstance(value, dict):
        raise ModelBackendError("model endpoint response is not a JSON object")
    return value


def _openai_chat_call(
    *, profile: ModelProfile, secret: str, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint = _endpoint(profile.base_url, "/chat/completions")
    system = (
        "Analyze only the supplied frozen evidence. Do not use tools, browsing, external "
        "knowledge retrieval, or unstated facts. Return one JSON object matching the schema."
    )
    user_prompt = prompt
    if profile.output_mode == "local_validated":
        user_prompt += "\n\nRequired JSON Schema:\n" + json.dumps(schema, sort_keys=True)
    response_format: dict[str, Any]
    if profile.output_mode == "strict":
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "shaq_daily_oracle_output",
                "strict": True,
                "schema": schema,
            },
        }
    else:
        response_format = {"type": "json_object"}
    payload: dict[str, Any] = {
        "model": profile.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": response_format,
        "max_tokens": profile.maximum_output_tokens,
    }
    value = _http_post_json(
        url=endpoint,
        headers={**_auth_headers(profile, secret), "Content-Type": "application/json"},
        payload=payload,
        timeout=profile.timeout_seconds,
    )
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelBackendError("Chat Completions response has no choices")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    parsed = _json_from_text(message.get("content"))
    return parsed, {
        "backend": "openai-chat-completions",
        "response_id": str(value.get("id", "")),
        "response_model": str(value.get("model", profile.model)),
        "usage": value.get("usage") if isinstance(value.get("usage"), dict) else None,
        "request_policy": {"tools": [], **_effective_protocol_policy(profile)},
        "schema_enforcement": (
            "native" if profile.output_mode == "strict" else "local_validation"
        ),
    }


def _anthropic_call(
    *, profile: ModelProfile, secret: str, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint = _endpoint(profile.base_url, "/v1/messages")
    user_prompt = prompt
    payload: dict[str, Any] = {
        "model": profile.model,
        "max_tokens": profile.maximum_output_tokens,
        "system": (
            "Analyze only the supplied frozen evidence. Do not use tools, browsing, external "
            "knowledge retrieval, or unstated facts. Return only the requested structured result."
        ),
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if profile.output_mode == "strict":
        payload["output_config"] = {
            "format": {"type": "json_schema", "schema": schema}
        }
    else:
        payload["messages"][0]["content"] += (
            "\n\nReturn one JSON object matching this schema:\n"
            + json.dumps(schema, sort_keys=True)
        )
    headers = {
        **_auth_headers(profile, secret),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    value = _http_post_json(
        url=endpoint, headers=headers, payload=payload, timeout=profile.timeout_seconds
    )
    content = value.get("content")
    if not isinstance(content, list):
        raise ModelBackendError("Anthropic response has no content")
    text = next(
        (
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ),
        None,
    )
    parsed = _json_from_text(text)
    return parsed, {
        "backend": "anthropic-messages",
        "response_id": str(value.get("id", "")),
        "response_model": str(value.get("model", profile.model)),
        "usage": value.get("usage") if isinstance(value.get("usage"), dict) else None,
        "request_policy": {"tools": [], **_effective_protocol_policy(profile)},
        "schema_enforcement": (
            "native" if profile.output_mode == "strict" else "local_validation"
        ),
    }


def call_structured(
    *,
    profile: ModelProfile,
    secret: str,
    prompt: str,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call one configured backend with no tools and return a hash-bound audit."""

    profile.validate()
    estimated_input_tokens = max(1, len(prompt.encode("utf-8")) // 4)
    if estimated_input_tokens + profile.maximum_output_tokens > profile.maximum_context_tokens:
        raise ModelBackendError(
            "冻结证据超过该模型配置的上下文长度；请缩小证据或提高明确的上下文上限"
        )
    started = datetime.now(ZoneInfo("America/New_York")).isoformat()
    try:
        if profile.protocol == "codex-cli":
            parsed, provider_audit = _codex_cli_call(
                profile=profile, prompt=prompt, schema=schema
            )
        elif profile.protocol == "claude-code":
            parsed, provider_audit = _claude_code_call(
                profile=profile, prompt=prompt, schema=schema
            )
        elif profile.protocol == "openai-responses":
            parsed, provider_audit = _openai_responses_call(
                profile=profile, secret=secret, prompt=prompt, schema=schema
            )
        elif profile.protocol == "openai-chat-completions":
            parsed, provider_audit = _openai_chat_call(
                profile=profile, secret=secret, prompt=prompt, schema=schema
            )
        elif profile.protocol == "anthropic-messages":
            parsed, provider_audit = _anthropic_call(
                profile=profile, secret=secret, prompt=prompt, schema=schema
            )
        else:
            raise ModelBackendError(f"unsupported cross-platform protocol: {profile.protocol}")
    except ModelBackendError:
        raise
    except Exception as exc:
        detail = safe_model_error_summary(exc, sensitive_values=(secret,))
        raise ModelBackendError(f"model call failed: {type(exc).__name__}: {detail}") from exc
    returned_model = str(provider_audit.get("response_model", "")).strip()
    if (
        returned_model and profile.model != "subscription-default"
        and not _model_identity_matches(profile.model, returned_model)
    ):
        raise ModelBackendError("model endpoint returned a different model than configured")
    parsed = _validate_result(parsed, schema)
    completed = datetime.now(ZoneInfo("America/New_York")).isoformat()
    audit = {
        **provider_audit,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.identity(),
        "request_policy_sha256": profile.request_policy_identity(),
        "endpoint_fingerprint": profile.endpoint_fingerprint(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "schema_sha256": sha256_payload(schema),
        "output_sha256": sha256_payload(parsed),
        "started_at_et": started,
        "completed_at_et": completed,
        "secret_recorded": False,
        "model_facing_tools_allowed": False,
    }
    return parsed, audit


def probe_model_profile(*, profile: ModelProfile, secret: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status"],
        "properties": {"status": {"type": "string", "const": "ready"}},
    }
    if uses_local_subscription(profile):
        executable = _local_cli(profile)
        arguments = (
            ["login", "status"]
            if profile.protocol == "codex-cli"
            else ["auth", "status"]
        )
        command = _cli_command(executable, arguments)
        try:
            completed = subprocess.run(
                command, text=True, encoding="utf-8", errors="replace",
                capture_output=True, shell=False, timeout=10,
                env=_local_cli_environment(), check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelBackendError("本地模型登录检查超时") from exc
        if completed.returncode != 0:
            detail = safe_model_error_summary(completed.stderr or completed.stdout)
            raise ModelBackendError(f"本地模型登录检查失败：{detail}")
        status_text = (completed.stdout + "\n" + completed.stderr)
        if profile.protocol == "codex-cli" and "Logged in" not in status_text:
            raise ModelBackendError("Codex 尚未登录 ChatGPT 账号")
    result, audit = call_structured(
        profile=profile,
        secret=secret,
        prompt='Return the structured status {"status":"ready"}.',
        schema=schema,
    )
    if result != {"status": "ready"}:
        raise ModelBackendError("model capability probe returned an unexpected result")
    if uses_local_subscription(profile):
        audit = {**audit, "connection": "local-subscription-ready"}
    return audit
