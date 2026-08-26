from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload


class IdentityError(ValueError):
    """The formal prediction core changed inside a frozen campaign."""


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


def ensure_formal_core_lock(
    *, package_root: Path, runtime_root: Path, system_identity: str,
    freeze_start: date, freeze_end: date, observed_at: datetime,
) -> dict[str, Any]:
    if freeze_end < freeze_start:
        raise IdentityError("formal-core freeze dates are reversed")
    current = formal_core_sha256(package_root)
    path = runtime_root / "formal_core_lock.json"
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
