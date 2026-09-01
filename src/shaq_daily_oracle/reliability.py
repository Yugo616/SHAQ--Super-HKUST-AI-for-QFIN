from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .hashing import sha256_file, sha256_payload
from .identity import formal_core_sha256


class ReliabilityError(RuntimeError):
    """A release certificate or live reliability gate is invalid."""


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _declared_dependencies(package_root: Path) -> dict[str, str]:
    import tomllib

    project = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    requirements = list(project.get("dependencies", []))
    for rows in project.get("optional-dependencies", {}).values():
        requirements.extend(rows)
    versions: dict[str, str] = {}
    for requirement in requirements:
        name = re.split(r"[<>=!~;\[]", str(requirement), maxsplit=1)[0].strip()
        if not name:
            continue
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return dict(sorted(versions.items(), key=lambda row: row[0].lower()))


def release_subject(package_root: Path, ai_config_path: Path) -> dict[str, Any]:
    dependencies = _declared_dependencies(package_root)
    return {
        "formal_core_sha256": formal_core_sha256(package_root),
        "ai_backend_config_sha256": sha256_file(ai_config_path),
        "pyproject_sha256": sha256_file(package_root / "pyproject.toml"),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": dependencies,
        "dependencies_sha256": sha256_payload(dependencies),
    }


def _run_logged(
    *, command: list[str], package_root: Path, log_root: Path, name: str,
) -> dict[str, Any]:
    started = datetime.now(ZoneInfo("America/New_York"))
    result = subprocess.run(
        command, cwd=package_root.parent, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(package_root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    ended = datetime.now(ZoneInfo("America/New_York"))
    stdout_path = log_root / f"{name}.stdout.log"
    stderr_path = log_root / f"{name}.stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    return {
        "name": name,
        "command": command,
        "started_at_et": started.isoformat(),
        "ended_at_et": ended.isoformat(),
        "returncode": result.returncode,
        "stdout_file": stdout_path.name,
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_file": stderr_path.name,
        "stderr_sha256": sha256_file(stderr_path),
    }


def certify_release(
    *, package_root: Path, ai_config_path: Path, data_root: Path,
    repetitions: int,
) -> dict[str, Any]:
    reliability_config = _read(package_root / "config/reliability.json")
    required_repetitions = int(reliability_config["certification_repetitions"])
    if repetitions != required_repetitions:
        raise ReliabilityError(
            f"certification requires exactly {required_repetitions} stable runs"
        )
    subject = release_subject(package_root, ai_config_path)
    issued = datetime.now(ZoneInfo("America/New_York"))
    certification_id = f"{issued.strftime('%Y%m%dT%H%M%S%z')}-{subject['formal_core_sha256'][:12]}"
    log_root = data_root / "release_certifications" / certification_id
    log_root.mkdir(parents=True, exist_ok=False)
    runs = []
    for index in range(1, repetitions + 1):
        run = _run_logged(
            command=[
                sys.executable, "-m", "unittest", "discover", "-s",
                str(package_root / "tests"), "-v",
            ],
            package_root=package_root, log_root=log_root, name=f"tests-{index}",
        )
        output = (log_root / run["stdout_file"]).read_text(encoding="utf-8") + (
            log_root / run["stderr_file"]
        ).read_text(encoding="utf-8")
        count = re.search(r"Ran (\d+) tests?", output)
        run["test_count"] = int(count.group(1)) if count else None
        run["no_order_rehearsal_present"] = (
            "test_zero_forecast_paper_run_completes_without_waiting_for_close" in output
        )
        runs.append(run)
    release_validation = _run_logged(
        command=[sys.executable, str(package_root / "scripts/validate_release.py")],
        package_root=package_root, log_root=log_root, name="skill-validation",
    )
    passed = all(
        row["returncode"] == 0 and row["test_count"] and row["no_order_rehearsal_present"]
        for row in runs
    ) and release_validation["returncode"] == 0
    unsigned = {
        "schema_version": 1,
        "certification_id": certification_id,
        "issued_at_et": issued.isoformat(),
        "status": "passed" if passed else "failed",
        "subject": subject,
        "test_runs": runs,
        "skill_validation": release_validation,
        "references": [
            "REF-GOOGLE-SRE-RELEASE-001", "REF-SLSA-PROVENANCE-001",
        ],
    }
    certificate = {**unsigned, "certificate_sha256": sha256_payload(unsigned)}
    certificate_path = log_root / "release_certificate.json"
    _atomic_json(certificate_path, certificate)
    if not passed:
        raise ReliabilityError(f"release certification failed: {certificate_path}")
    _atomic_json(data_root / "release_certificate.json", certificate)
    return certificate


def verify_release_certificate(
    *, package_root: Path, ai_config_path: Path, certificate_path: Path,
) -> dict[str, Any]:
    if not certificate_path.is_file():
        raise ReliabilityError("release certificate is missing")
    certificate = _read(certificate_path)
    unsigned = dict(certificate)
    declared = unsigned.pop("certificate_sha256", None)
    if declared != sha256_payload(unsigned):
        raise ReliabilityError("release certificate hash mismatch")
    if certificate.get("status") != "passed":
        raise ReliabilityError("release certificate is not passed")
    if certificate.get("subject") != release_subject(package_root, ai_config_path):
        raise ReliabilityError("running code, configuration or dependencies differ from certification")
    test_runs = certificate.get("test_runs", [])
    required_repetitions = int(
        _read(package_root / "config/reliability.json")["certification_repetitions"]
    )
    if len(test_runs) != required_repetitions or any(
        row.get("returncode") != 0
        or not row.get("test_count")
        or row.get("no_order_rehearsal_present") is not True
        for row in test_runs
    ):
        raise ReliabilityError("release certificate does not contain stable green test runs")
    if certificate.get("skill_validation", {}).get("returncode") != 0:
        raise ReliabilityError("release certificate has no green Skill validation")
    return certificate


def write_release_receipt(
    *, output: Path, package_root: Path, ai_config_path: Path, certificate_path: Path,
) -> dict[str, Any]:
    certificate = verify_release_certificate(
        package_root=package_root, ai_config_path=ai_config_path,
        certificate_path=certificate_path,
    )
    counts = [int(row["test_count"]) for row in certificate["test_runs"]]
    receipt = {
        "schema_version": 7,
        "public_passed": True,
        "release_validator_passed": True,
        "plugin_validator_passed": True,
        "legacy_passed": None,
        "public_test_count": counts[-1],
        "certification_repetitions": len(counts),
        "release_certificate_valid": True,
        "release_certificate_path": str(certificate_path.resolve()),
        "release_certificate_sha256": sha256_file(certificate_path),
        "certificate_sha256": certificate["certificate_sha256"],
    }
    _atomic_json(output, receipt)
    return receipt


def certificate_path_for_runtime(runtime_root: Path) -> Path:
    configured = os.environ.get("DAILY_ORACLE_RELEASE_CERTIFICATE")
    return (
        Path(configured).expanduser().resolve()
        if configured else runtime_root.parent / "release_certificate.json"
    )


def minimum_disk_ready(path: Path, minimum_free_gib: float) -> tuple[bool, str]:
    free = shutil.disk_usage(path).free / (1024 ** 3)
    return free >= minimum_free_gib, f"{free:.1f} GiB free; minimum {minimum_free_gib:.1f} GiB"
