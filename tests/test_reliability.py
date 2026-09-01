from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.hashing import sha256_payload  # noqa: E402
from shaq_daily_oracle.reliability import (  # noqa: E402
    ReliabilityError,
    certify_release,
    release_subject,
    verify_release_certificate,
    write_release_receipt,
)


class ReliabilityTests(unittest.TestCase):
    def package(self, root: Path) -> tuple[Path, Path]:
        package = root / "package"
        (package / "config").mkdir(parents=True)
        (package / "governance").mkdir()
        (package / "scripts").mkdir()
        (package / "tests").mkdir()
        (package / "pyproject.toml").write_text(
            "[project]\nname='reliability-fixture'\nversion='1'\ndependencies=[]\n",
            encoding="utf-8",
        )
        ai = package / "config/ai.json"
        ai.write_text('{"backend":"codex-cli","model":"test"}\n', encoding="utf-8")
        (package / "config/policy.json").write_text('{"value":1}\n', encoding="utf-8")
        (package / "config/reliability.json").write_text(
            '{"certification_repetitions":3}\n', encoding="utf-8"
        )
        (package / "governance/formal-core-manifest.json").write_text(json.dumps({
            "include_patterns": ["config/*.json"],
        }), encoding="utf-8")
        (package / "scripts/validate_release.py").write_text("", encoding="utf-8")
        return package, ai

    def certificate(self, package: Path, ai: Path) -> dict:
        unsigned = {
            "schema_version": 1,
            "certification_id": "fixture",
            "issued_at_et": "2026-09-01T18:00:00-04:00",
            "status": "passed",
            "subject": release_subject(package, ai),
            "test_runs": [{
                "returncode": 0, "test_count": 10,
                "no_order_rehearsal_present": True,
            }] * 3,
            "skill_validation": {"returncode": 0},
            "references": ["REF-GOOGLE-SRE-RELEASE-001"],
        }
        return {**unsigned, "certificate_sha256": sha256_payload(unsigned)}

    def test_certificate_is_bound_to_code_config_dependencies_and_ai(self):
        with tempfile.TemporaryDirectory() as name:
            package, ai = self.package(Path(name))
            certificate = Path(name) / "certificate.json"
            certificate.write_text(json.dumps(self.certificate(package, ai)), encoding="utf-8")
            verify_release_certificate(
                package_root=package, ai_config_path=ai, certificate_path=certificate
            )
            (package / "config/policy.json").write_text('{"value":2}\n', encoding="utf-8")
            with self.assertRaises(ReliabilityError):
                verify_release_certificate(
                    package_root=package, ai_config_path=ai, certificate_path=certificate
                )

    def test_runtime_receipt_reuses_certificate_without_running_tests(self):
        with tempfile.TemporaryDirectory() as name:
            package, ai = self.package(Path(name))
            certificate = Path(name) / "certificate.json"
            certificate.write_text(json.dumps(self.certificate(package, ai)), encoding="utf-8")
            receipt = write_release_receipt(
                output=Path(name) / "tests_report.json", package_root=package,
                ai_config_path=ai, certificate_path=certificate,
            )
            self.assertTrue(receipt["release_certificate_valid"])
            self.assertEqual(receipt["certification_repetitions"], 3)

    def test_certification_keeps_full_logs_and_requires_every_repetition(self):
        with tempfile.TemporaryDirectory() as name:
            package, ai = self.package(Path(name))
            output = (
                "test_zero_forecast_paper_run_completes_without_waiting_for_close ... ok\n"
                "Ran 10 tests in 0.1s\n\nOK\n"
            )
            with patch(
                "shaq_daily_oracle.reliability.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""),
            ):
                certificate = certify_release(
                    package_root=package, ai_config_path=ai,
                    data_root=Path(name) / "data", repetitions=3,
                )
            self.assertEqual(len(certificate["test_runs"]), 3)
            log_root = (
                Path(name) / "data" / "release_certifications"
                / certificate["certification_id"]
            )
            self.assertTrue((log_root / "tests-1.stdout.log").is_file())
            self.assertTrue((Path(name) / "data" / "release_certificate.json").is_file())

    def test_failed_recertification_cannot_replace_current_green_certificate(self):
        with tempfile.TemporaryDirectory() as name:
            package, ai = self.package(Path(name))
            data_root = Path(name) / "data"
            current = data_root / "release_certificate.json"
            current.parent.mkdir(parents=True)
            current.write_text(json.dumps(self.certificate(package, ai)), encoding="utf-8")
            original = current.read_bytes()
            failed_output = "Ran 10 tests in 0.1s\n\nFAILED (failures=1)\n"
            with patch(
                "shaq_daily_oracle.reliability.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1, stdout=failed_output, stderr="named failure"
                ),
            ):
                with self.assertRaises(ReliabilityError):
                    certify_release(
                        package_root=package, ai_config_path=ai,
                        data_root=data_root, repetitions=3,
                    )
            self.assertEqual(current.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
