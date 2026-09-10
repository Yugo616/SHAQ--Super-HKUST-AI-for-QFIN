import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from shaq_daily_oracle.skill_versions import (
    ALLOWED_SKILL_RELATIVE_PATHS,
    LocalSkillRegistry,
    SkillVersionError,
)


PACKAGE_ROOT = Path(__file__).parents[1]
SYNTHESIS_SHA256 = "2c5d60f35dbefaefc9f521b6c18c7d5b0969dc48b0b5f2b68c65aa44933c3646"
BASELINE_ID = "independent-gate-1"
SHADOW_ID = "cross-domain-synthesis-1"


class BundledVersionTests(unittest.TestCase):
    def test_first_start_installs_full_shadow_and_restart_preserves_it(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions
        with tempfile.TemporaryDirectory() as d:
            registry = LocalSkillRegistry(root=Path(d), package_skills=PACKAGE_ROOT / 'skills')
            install_bundled_versions(registry)
            self.assertEqual(
                {v['version_id'] for v in registry.list_versions()},
                {'main', 'synthesis-1', BASELINE_ID, SHADOW_ID},
            )
            docs = registry.effective_skills('synthesis-1', 'team')
            self.assertEqual(json.loads(docs['decision/cases.json'])['mode'], 'synthesis')
            self.assertEqual(len([p for p in docs if p.endswith('/SKILL.md')]), 8)
            before = {str(p): p.read_bytes() for p in Path(d).rglob('*') if p.is_file()}
            install_bundled_versions(registry)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(d).rglob('*') if p.is_file()})

    def test_original_synthesis_payload_is_byte_preserved(self):
        payload = (PACKAGE_ROOT / "bundled_versions/synthesis.json").read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), SYNTHESIS_SHA256)

    def test_release_methods_have_separate_names_badges_aliases_and_complete_content(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions

        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(root=Path(name), package_skills=PACKAGE_ROOT / "skills")
            install_bundled_versions(registry)
            rows = {row["version_id"]: row for row in registry.list_method_versions()}

            self.assertEqual(set(rows), {BASELINE_ID, SHADOW_ID})
            self.assertEqual(
                (rows[BASELINE_ID]["method_name"], rows[BASELINE_ID]["status_badge"]),
                ("独立证据门禁版", "正式基准"),
            )
            self.assertEqual(
                (rows[SHADOW_ID]["method_name"], rows[SHADOW_ID]["status_badge"]),
                ("跨域综合研判版", "Shadow"),
            )
            self.assertEqual(rows[BASELINE_ID]["aliases"], ["main"])
            self.assertEqual(rows[SHADOW_ID]["aliases"], ["synthesis-1"])
            for version_id in rows:
                documents = registry.effective_skills(version_id, "team")
                self.assertEqual(set(documents), ALLOWED_SKILL_RELATIVE_PATHS)
                self.assertEqual(len([p for p in documents if p.endswith("/SKILL.md")]), 8)
                self.assertEqual(len([p for p in documents if p.startswith("modules/")]), 14)

    def test_complete_packages_are_checkout_independent_and_installed_byte_for_byte(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package = root / "package"
            (package / "skills").mkdir(parents=True)
            shutil.copytree(PACKAGE_ROOT / "bundled_versions", package / "bundled_versions")
            registry = LocalSkillRegistry(root=root / "registry", package_skills=package / "skills")
            install_bundled_versions(registry)

            for filename, version_id in (
                ("independent-gate.json", BASELINE_ID),
                ("cross-domain-synthesis.json", SHADOW_ID),
            ):
                embedded = json.loads((package / "bundled_versions" / filename).read_text())
                effective = registry.effective_skills(version_id, "team")
                self.assertEqual(effective, embedded["files"])
                target = registry.root / "team" / version_id
                for relative_path, content in embedded["files"].items():
                    from shaq_daily_oracle.skill_versions import _stored_artifact_path
                    self.assertEqual(
                        (target / _stored_artifact_path(relative_path)).read_bytes(),
                        content.encode("utf-8"),
                    )

    def test_complete_package_tampering_and_incomplete_method_manifest_are_rejected(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions
        from shaq_daily_oracle.skill_versions import SkillVersionManifest

        package = json.loads(
            (PACKAGE_ROOT / "bundled_versions/independent-gate.json").read_text()
        )
        manifest = SkillVersionManifest.from_dict(package["manifest"])
        with self.assertRaisesRegex(SkillVersionError, "complete method"):
            SkillVersionManifest.create(
                version_id="incomplete-method",
                author="team",
                base_main_sha="a" * 40,
                files={
                    path: package["files"][path]
                    for path in ("decision/decision.js", "decision/cases.json")
                },
                description="Incomplete.",
                method_name="Incomplete",
                status_badge="Shadow",
                aliases=(),
            )

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package_root = root / "package"
            shutil.copytree(PACKAGE_ROOT / "skills", package_root / "skills")
            (package_root / "bundled_versions").mkdir()
            package["files"]["decision/decision.js"] += "\n// tampered"
            (package_root / "bundled_versions/independent-gate.json").write_text(
                json.dumps(package), encoding="utf-8"
            )
            registry = LocalSkillRegistry(root=root / "registry", package_skills=package_root / "skills")
            with self.assertRaisesRegex(SkillVersionError, "differs"):
                install_bundled_versions(registry)

        self.assertEqual(
            manifest.base_main_sha,
            "967d9bef3be85759760e8bf4a93e53656769c62a2c6612fd9dae7263563d3d52",
        )

    def test_old_ids_and_drafts_remain_compatible(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions

        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(root=Path(name), package_skills=PACKAGE_ROOT / "skills")
            install_bundled_versions(registry)
            old_main = registry.effective_skills("main", "team")
            old_synthesis = registry.effective_skills("synthesis-1", "team")
            self.assertEqual(json.loads(old_synthesis["decision/cases.json"])["mode"], "synthesis")
            root = registry.save_draft(
                author="local", draft_id="old-main-copy", files=old_main,
                base_main_sha=registry.main_version()["version_sha256"],
            )
            metadata, files = registry.load_draft(author="local", draft_id="old-main-copy")
            self.assertEqual(root.name, "old-main-copy")
            self.assertEqual(metadata["draft_id"], "old-main-copy")
            self.assertEqual(files, old_main)

    def test_method_comparison_contains_real_rule_changes(self):
        from shaq_daily_oracle.bundled_versions import install_bundled_versions

        with tempfile.TemporaryDirectory() as name:
            registry = LocalSkillRegistry(root=Path(name), package_skills=PACKAGE_ROOT / "skills")
            install_bundled_versions(registry)
            comparison = registry.compare_methods(BASELINE_ID, SHADOW_ID, author="team")

        self.assertIn("decision/decision.js", comparison["changed_paths"])
        self.assertIn("skills/daily-oracle/SKILL.md", comparison["changed_paths"])
        self.assertGreater(comparison["changed_file_count"], 2)
        self.assertNotEqual(comparison["decision_mode"]["left"], comparison["decision_mode"]["right"])

    def test_packages_adopt_exact_current_methods_modules_and_parameters(self):
        from shaq_daily_oracle.decision_sandbox import decision_parameters
        from shaq_daily_oracle.module_rules import MODULES, default_rule

        baseline = json.loads(
            (PACKAGE_ROOT / "bundled_versions/independent-gate.json").read_text()
        )["files"]
        shadow = json.loads(
            (PACKAGE_ROOT / "bundled_versions/cross-domain-synthesis.json").read_text()
        )["files"]
        legacy_synthesis = json.loads(
            (PACKAGE_ROOT / "bundled_versions/synthesis.json").read_text()
        )["files"]

        for relative_path, content in baseline.items():
            if relative_path.startswith(("skills/", "decision/")):
                self.assertEqual(
                    content,
                    (PACKAGE_ROOT / relative_path).read_text(encoding="utf-8"),
                    relative_path,
                )
        for relative_path, content in legacy_synthesis.items():
            self.assertEqual(shadow[relative_path], content, relative_path)
        for module in MODULES:
            path = f"modules/{module}/compute.js"
            self.assertEqual(baseline[path], default_rule(module))
            self.assertEqual(shadow[path], default_rule(module))
            self.assertEqual(baseline[f"modules/{module}/cases.json"], shadow[f"modules/{module}/cases.json"])
        self.assertEqual(decision_parameters(baseline["decision/cases.json"]), {"maximum_predictions": 3})
        self.assertEqual(decision_parameters(shadow["decision/cases.json"]), {"maximum_predictions": 3})
