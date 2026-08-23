#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, env: dict[str, str] | None = None) -> tuple[bool, int | None]:
    result = subprocess.run(command, cwd=ROOT.parent, env=env, capture_output=True, text=True)
    combined = result.stdout + "\n" + result.stderr
    match = re.search(r"Ran (\d+) tests?", combined)
    return result.returncode == 0, (int(match.group(1)) if match else None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Daily Oracle public acceptance tests")
    parser.add_argument("--legacy-tests", type=Path)
    parser.add_argument("--plugin-validator", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    public_passed, public_count = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"],
        env=environment,
    )
    release_passed, _ = _run([sys.executable, str(ROOT / "scripts/validate_release.py")])
    legacy_passed = legacy_count = None
    if args.legacy_tests:
        legacy_passed, legacy_count = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(args.legacy_tests), "-v"]
        )
    plugin_passed = None
    if args.plugin_validator:
        plugin_passed = all(
            _run([sys.executable, str(args.plugin_validator), str(skill.parent)])[0]
            for skill in sorted((ROOT / "skills").glob("*/SKILL.md"))
        )
    payload = {
        "schema_version": 6,
        "public_passed": public_passed,
        "public_test_count": public_count,
        "release_validator_passed": release_passed,
        "plugin_validator_passed": plugin_passed,
        "legacy_passed": legacy_passed,
        "legacy_test_count": legacy_count,
    }
    if args.output.exists():
        raise FileExistsError("acceptance test report is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    required_results = [public_passed, release_passed]
    if args.legacy_tests:
        required_results.append(legacy_passed)
    if args.plugin_validator:
        required_results.append(plugin_passed)
    if not all(value is True for value in required_results):
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
