from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
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
}
SUPPORTED_OUTPUT_MODES = {"strict", "local_validated"}


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
        if self.protocol != "codex-cli":
            parsed = urlparse(self.base_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ModelBackendError("remote model base_url must be an absolute HTTPS URL")
        if self.auth_style not in {"bearer", "x-api-key"}:
            raise ModelBackendError("unsupported model authentication style")
        for name in (
            "timeout_seconds",
            "maximum_output_tokens",
            "max_concurrency",
            "rate_limit_per_minute",
        ):
            if int(getattr(self, name)) <= 0:
                raise ModelBackendError(f"{name} must be positive")
        for price in (self.input_price_per_million, self.output_price_per_million):
            if price is not None and float(price) < 0:
                raise ModelBackendError("token prices cannot be negative")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> str:
        return sha256_payload(self.public_dict())

    def endpoint_fingerprint(self) -> str:
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
        raise ModelBackendError(f"model output failed JSON Schema validation: {exc}") from exc
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
        "request_policy": {"store": False, "tools": [], "tool_choice": "none"},
        "schema_enforcement": "native",
    }


def _http_post_json(
    *, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except ImportError as exc:
        raise ModelBackendError("httpx is unavailable") from exc
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        value = response.json()
    except Exception as exc:
        raise ModelBackendError(f"model endpoint request failed: {type(exc).__name__}: {exc}") from exc
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
        "temperature": 0,
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
        "request_policy": {"tools": [], "temperature": 0},
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
        "request_policy": {"tools": []},
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
    if profile.protocol == "codex-cli":
        raise ModelBackendError("Codex CLI is not part of the cross-platform API adapter")
    started = datetime.now(ZoneInfo("America/New_York")).isoformat()
    try:
        if profile.protocol == "openai-responses":
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
        raise ModelBackendError(f"model call failed: {type(exc).__name__}: {exc}") from exc
    returned_model = str(provider_audit.get("response_model", "")).strip()
    if returned_model and not _model_identity_matches(profile.model, returned_model):
        raise ModelBackendError(
            f"model endpoint returned a different model: expected {profile.model}, got {returned_model}"
        )
    parsed = _validate_result(parsed, schema)
    completed = datetime.now(ZoneInfo("America/New_York")).isoformat()
    audit = {
        **provider_audit,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.identity(),
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
    result, audit = call_structured(
        profile=profile,
        secret=secret,
        prompt='Return the structured status {"status":"ready"}.',
        schema=schema,
    )
    if result != {"status": "ready"}:
        raise ModelBackendError("model capability probe returned an unexpected result")
    return audit
