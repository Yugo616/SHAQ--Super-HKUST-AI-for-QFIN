from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload


class IdentityError(ValueError):
    """The formal prediction core changed inside a frozen campaign."""


def resolve_system_identity(config: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    """Resolve the latest identity already effective at the requested ET timestamp."""

    entries = config.get("identities")
    if not isinstance(entries, list):
        entries = [{key: value for key, value in config.items() if key != "parameter_bindings"}]
    eligible = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("identity", "")).strip():
            raise IdentityError("system identity history contains an invalid entry")
        effective = datetime.fromisoformat(str(entry.get("effective_from_et", "")).replace("Z", "+00:00"))
        if effective.tzinfo is None or effective.utcoffset() is None:
            raise IdentityError("system identity effective time requires an offset")
        if effective <= observed_at:
            eligible.append((effective, entry))
    if not eligible:
        raise IdentityError("no system identity is effective for the requested session")
    return dict(max(eligible, key=lambda item: item[0])[1])


def resolve_runtime_identity(
    config: dict[str, Any], observed_at: datetime, ai_config: dict[str, Any],
) -> dict[str, Any]:
    """Bind the governed research identity to the exact inference backend config."""

    base = resolve_system_identity(config, observed_at)
    backend = str(ai_config.get("backend", "")).strip()
    if not backend:
        raise IdentityError("runtime identity requires a named AI backend")
    safe_backend = re.sub(r"[^A-Za-z0-9._-]+", "-", backend).strip("-")
    if not safe_backend:
        raise IdentityError("AI backend cannot form a runtime identity")
    backend_config_sha256 = sha256_payload(ai_config)
    return {
        **base,
        "base_identity": base["identity"],
        "identity": f"{base['identity']}--{safe_backend}--{backend_config_sha256[:12]}",
        "ai_backend": backend,
        "ai_backend_config_sha256": backend_config_sha256,
    }


def formal_core_sha256(package_root: Path) -> str:
    manifest_path = package_root / "governance/formal-core-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    patterns = manifest.get("include_patterns", [])
    if not isinstance(patterns, list) or not patterns:
        raise IdentityError("formal-core manifest has no include patterns")
    files: set[Path] = {manifest_path}
    for pattern in patterns:
        matched = {path for path in package_root.glob(str(pattern)) if path.is_file()}
        if not matched:
            raise IdentityError(f"formal-core pattern matched no files: {pattern}")
        files.update(matched)
    return sha256_payload({
        str(path.relative_to(package_root)): sha256_file(path)
        for path in sorted(files)
    })


def formal_core_lock_path(runtime_root: Path, system_identity: str) -> Path:
    legacy = runtime_root / "formal_core_lock.json"
    if not legacy.exists():
        return legacy
    try:
        saved_identity = json.loads(legacy.read_text(encoding="utf-8")).get("system_identity")
    except (OSError, json.JSONDecodeError):
        saved_identity = None
    if saved_identity == system_identity:
        return legacy
    safe_identity = re.sub(r"[^A-Za-z0-9._-]+", "-", system_identity).strip("-")
    if not safe_identity:
        raise IdentityError("system identity cannot form a lock name")
    return runtime_root / "formal_core_locks" / f"{safe_identity}.json"


def ensure_formal_core_lock(
    *, package_root: Path, runtime_root: Path, system_identity: str,
    freeze_start: date, freeze_end: date, observed_at: datetime,
) -> dict[str, Any]:
    if freeze_end < freeze_start:
        raise IdentityError("formal-core freeze dates are reversed")
    current = formal_core_sha256(package_root)
    path = formal_core_lock_path(runtime_root, system_identity)
    expected = {
        "schema_version": 1,
        "system_identity": system_identity,
        "formal_core_sha256": current,
        "freeze_start": freeze_start.isoformat(),
        "freeze_end": freeze_end.isoformat(),
        "created_at_et": observed_at.isoformat(),
        "policy": "retrospective_results_cannot_mutate_formal_prediction_core",
    }
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return expected
    saved = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "schema_version", "system_identity", "formal_core_sha256",
        "freeze_start", "freeze_end", "policy",
    ):
        if saved.get(key) != expected.get(key):
            raise IdentityError("formal prediction core differs from the campaign lock")
    return saved
