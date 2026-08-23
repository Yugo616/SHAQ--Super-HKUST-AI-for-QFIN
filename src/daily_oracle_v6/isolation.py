from __future__ import annotations

from typing import Any


class IsolationError(ValueError):
    """Formal AI cannot be used without an enforceable inference boundary."""


REQUIRED_CAPABILITIES = (
    "evidence_read_only",
    "labels_unmounted",
    "network_denied",
    "tools_denied",
)


def formal_ai_status() -> dict[str, Any]:
    """Return the capability status of the public package's inference runner.

    This default is deliberately disabled. The macOS backend may only become
    formal after ``snapshot_isolation.py --backend sandboxed-codex`` writes an
    immutable, runtime-specific kernel-sandbox attestation.
    """

    return {
        "schema_version": 6,
        "backend": "unavailable",
        "formal_ai_enabled": False,
        "evidence_read_only": False,
        "labels_unmounted": False,
        "network_denied": False,
        "tools_denied": False,
        "reason": "no verified runtime-specific isolation attestation was supplied",
    }


def validate_isolation_status(status: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "backend", "formal_ai_enabled", "reason",
        *REQUIRED_CAPABILITIES,
    }
    if set(status) != expected or status.get("schema_version") != 6:
        raise IsolationError("isolation status differs from the public contract")
    if not isinstance(status.get("formal_ai_enabled"), bool):
        raise IsolationError("formal_ai_enabled must be boolean")
    if not isinstance(status.get("backend"), str) or not status["backend"].strip():
        raise IsolationError("isolation backend must be named")
    if not isinstance(status.get("reason"), str) or not status["reason"].strip():
        raise IsolationError("isolation status requires a reason")
    if any(not isinstance(status.get(name), bool) for name in REQUIRED_CAPABILITIES):
        raise IsolationError("isolation capabilities must be boolean")
    if status["formal_ai_enabled"] and (
        status["backend"] == "unavailable"
        or not all(status[name] for name in REQUIRED_CAPABILITIES)
    ):
        raise IsolationError("formal AI requires every enforced isolation capability")
    return {key: status[key] for key in sorted(status)}
