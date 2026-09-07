from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.hashing import sha256_payload
from shaq_daily_oracle.model_backends import ModelProfile
from shaq_daily_oracle.research_batch import (
    ResearchBatchError,
    ResearchBatchRunner,
    VariantSelection,
    freeze_evidence_bundle,
    load_frozen_evidence,
)
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
from shaq_daily_oracle.research_dashboard import ResearchDashboardError
from shaq_daily_oracle.skill_versions import (
    LocalSkillRegistry,
    SkillVersionManifest,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class FakeModel:
    def __init__(self, fail_marker: str = "") -> None:
        self.calls = 0
        self.fail_marker = fail_marker

    def __call__(self, *, profile, secret, prompt, schema):
        self.calls += 1
        if self.fail_marker and self.fail_marker in prompt:
            raise RuntimeError("injected variant failure")
        if "FROZEN TASKS:" in prompt:
            tasks = json.loads(prompt.split("FROZEN TASKS:\n", 1)[1])
            results = []
            for task in tasks:
                domain = task["domain"]
                component = {
                    "market": "market_beta", "relationships": "industry_spillover",
                    "event": "company_event", "capital": "capital_flow",
                    "derivatives": "derivatives_distribution",
                    "price_volume": "price_volume_state",
                }[domain]
                cited = [row["evidence_id"] for row in task["evidence"] if (
                    row["evidence_id"].startswith("ev_market") if domain == "market"
                    else row["evidence_id"].startswith("ev_price")
                )]
                results.append({
                    "task_id": task["task_id"],
                    "report": {
                        "domain": domain, "as_of_et": task["as_of_et"],
                        "horizon": task["horizon"], "availability": "available",
                        "verdict": "bullish", "component_type": component,
                        "thesis": "冻结证据支持开盘到收盘偏强。",
                        "antithesis": "冲击也可能在开盘前已被消化。",
                        "unknowns": [], "invalidation": ["价格反应失去延续"],
                        "evidence_ids": cited, "lineage_root_ids": [],
                    },
                })
            result = {"results": results}
        else:
            reports = json.loads(prompt.split("REPORTS:\n", 1)[1])
            result = {"results": [{
                "symbol": symbol,
                "report": {
                    "counts_as_vote": False, "new_evidence_allowed": False,
                    "duplicate_lineage_roots": [], "unresolved_conflicts": [],
                    "strongest_countercase": "盘前冲击可能已被价格吸收。",
                    "veto": False, "veto_reason": "",
                },
            } for symbol in sorted(reports)]}
        return result, {
            "profile_sha256": profile.identity(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": sha256_payload(schema),
            "output_sha256": sha256_payload(result),
        }


def replacement_skill(name: str, marker: str = "") -> str:
    return (
        "---\n" f"name: {name}\n"
        "description: Governed Shadow research instructions.\n"
        "---\n\n# Shadow\n\nUse only frozen evidence. " + marker + "\n"
    )


class ResearchBatchTests(unittest.TestCase):
    def evidence(self, root: Path):
        as_of = "2026-09-04T08:49:00-04:00"
        return freeze_evidence_bundle(
            root=root / "evidence",
            as_of_et=as_of,
            scheduled_cutoff_et="2026-09-04T08:50:00-04:00",
            cutoff_status="on_time",
            candidates=[{
                "symbol": "AAPL", "gics_sector": "Information Technology",
                "captured_primary_event": False,
            }],
            records=[
                {
                    "evidence_id": "ev_market_1", "domain": "market",
                    "provider": "test-market", "source_uri": "https://example.test/market",
                    "captured_at": as_of, "raw_file_path": "raw/market.json",
                    "scope_symbols": ["*"],
                    "consumer_domains": ["market", "price_volume"],
                    "root_component_type": "market_context",
                },
                {
                    "evidence_id": "ev_price_aapl", "domain": "price_volume",
                    "provider": "test-bars", "source_uri": "https://example.test/aapl",
                    "captured_at": as_of, "raw_file_path": "raw/aapl.json",
                    "scope_symbols": ["AAPL"],
                    "consumer_domains": ["market", "price_volume"],
                    "root_component_type": "stock_price_volume",
                },
            ],
            files={
                "raw/market.json": b'{"SPY":{"return":0.01}}',
                "raw/aapl.json": b'{"symbol":"AAPL","gap":0.02}',
            },
            provider_manifest={"profile_id": "test", "order_flow": "unavailable"},
        )

    def registry(self, root: Path) -> LocalSkillRegistry:
        return LocalSkillRegistry(root=root / "registry", package_skills=PACKAGE_ROOT / "skills")

    def policy(self):
        return json.loads((PACKAGE_ROOT / "config/integration.json").read_text())

    def profile(self):
        return ModelProfile(
            profile_id="test", protocol="openai-chat-completions",
            base_url="https://model.test/v1", model="test", output_mode="strict",
        )

    def main_variant(self, registry: LocalSkillRegistry) -> VariantSelection:
        return VariantSelection.from_registry_row(registry.main_version())

    def install_shadow(
        self, registry: LocalSkillRegistry, *, version_id: str, domain: str, marker: str = ""
    ) -> VariantSelection:
        path = f"skills/{domain}/SKILL.md"
        files = {path: replacement_skill(domain, marker)}
        manifest = SkillVersionManifest.create(
            version_id=version_id, author="alice", base_main_sha="a" * 40,
            files=files, description="A test-only governed Skill change.",
            created_at="2026-09-04T00:00:00+00:00",
        )
        registry.install(manifest=manifest, files=files, commit_sha="b" * 40)
        row = next(value for value in registry.list_versions() if value["version_id"] == version_id)
        return VariantSelection.from_registry_row(row)

    def test_all_variants_share_one_frozen_candidate_set_and_research_never_orders(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            registry = self.registry(root)
            evidence = self.evidence(root)
            shadow = self.install_shadow(
                registry, version_id="event-wording", domain="primary-event-reasoner"
            )
            second_shadow = self.install_shadow(
                registry, version_id="market-wording", domain="market-common-shock"
            )
            runner = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            )
            model = FakeModel()
            result = runner.run(
                evidence=evidence,
                variants=[shadow, second_shadow, self.main_variant(registry)],
                profile=self.profile(), secret="secret", caller=model,
            )
            batch_root = Path(result["batch_root"])
            skill_snapshot_count = len(list((batch_root / "skills").glob("*.json")))
            model_call_count = len(list((batch_root / "model_calls").glob("*.json")))
            call = json.loads(next((batch_root / "model_calls").glob("*.json")).read_text())
            skill_snapshot = json.loads(
                next((batch_root / "skills").glob("*.json")).read_text()
            )
        self.assertTrue(result["status"]["all_variants_completed"])
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(model.calls, 4)  # changed market prompt; shared price and audit calls are cached
        self.assertEqual(result["manifest"]["schema_version"], 2)
        self.assertEqual(result["status"]["schema_version"], 2)
        self.assertEqual(result["status"]["skill_snapshot_count"], 3)
        self.assertEqual(skill_snapshot_count, 3)
        self.assertEqual(model_call_count, model.calls)
        self.assertIn("prompt", call)
        self.assertIn("schema", call)
        self.assertIn("result", call)
        self.assertEqual(len(skill_snapshot["documents"]), 16)
        for variant in result["results"].values():
            self.assertEqual(variant["candidate_set_sha256"], evidence.manifest["candidate_set_sha256"])
            self.assertEqual(variant["orders"], [])
            self.assertFalse(variant["broker_modules_loaded"])
            self.assertEqual(variant["predictions"][0]["symbol"], "AAPL")

    def test_variant_order_does_not_change_batch_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            registry = self.registry(root)
            evidence = self.evidence(root)
            shadow = self.install_shadow(
                registry, version_id="event-wording", domain="primary-event-reasoner"
            )
            runner = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            )
            model = FakeModel()
            first = runner.run(
                evidence=evidence, variants=[self.main_variant(registry), shadow],
                profile=self.profile(), secret="secret", caller=model,
            )
            second = runner.run(
                evidence=evidence, variants=[shadow, self.main_variant(registry)],
                profile=self.profile(), secret="secret", caller=model,
            )
        self.assertEqual(first["manifest"]["batch_identity_sha256"], second["manifest"]["batch_identity_sha256"])
        self.assertEqual(first["batch_root"], second["batch_root"])
        self.assertEqual(model.calls, 3)

    def test_one_bad_shadow_does_not_fail_main(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            registry = self.registry(root)
            evidence = self.evidence(root)
            bad = self.install_shadow(
                registry, version_id="bad-market", domain="market-common-shock",
                marker="FAIL_THIS_VARIANT",
            )
            runner = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            )
            result = runner.run(
                evidence=evidence, variants=[bad, self.main_variant(registry)],
                profile=self.profile(), secret="secret",
                caller=FakeModel(fail_marker="FAIL_THIS_VARIANT"),
            )
        self.assertIn("team/main", result["results"])
        self.assertIn("alice/bad-market", result["status"]["failed_variants"])
        self.assertEqual(result["results"]["team/main"]["orders"], [])

    def test_tampering_with_frozen_evidence_fails_before_analysis(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            evidence = self.evidence(root)
            (evidence.root / "raw/aapl.json").write_text('{"tampered":true}', encoding="utf-8")
            with self.assertRaises(ResearchBatchError):
                load_frozen_evidence(evidence.root)

    def test_duplicate_selected_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            registry = self.registry(root)
            evidence = self.evidence(root)
            main = self.main_variant(registry)
            runner = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            )
            with self.assertRaises(ResearchBatchError):
                runner.run(
                    evidence=evidence, variants=[main, main], profile=self.profile(),
                    secret="secret", caller=FakeModel(),
                )

    def test_disposable_dashboard_rebuilds_from_immutable_batch_files(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            registry = self.registry(root)
            staged = self.evidence(root / "staged")
            evidence_root = root / "evidence" / staged.manifest["evidence_hash"]
            evidence_root.parent.mkdir(parents=True)
            staged.root.replace(evidence_root)
            evidence = load_frozen_evidence(evidence_root)
            runner = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            )
            result = runner.run(
                evidence=evidence, variants=[self.main_variant(registry)],
                profile=self.profile(), secret="secret", caller=FakeModel(),
            )
            database = root / "index.sqlite3"
            dashboard = ResearchDashboardIndex(
                batches_root=root / "batches", database=database
            )
            first = dashboard.overview()
            database.unlink()
            second = dashboard.overview()
        self.assertEqual(first["batches"], second["batches"])
        self.assertEqual(first["latest"]["batch_id"], result["status"]["batch_id"])
        self.assertEqual(len(first["latest"]["variants"]), 1)
        self.assertGreater(len(first["latest"]["model_calls"]), 0)
        self.assertEqual(len(first["latest"]["skill_snapshots"]), 1)

    def test_dashboard_rejects_tampered_model_prompt_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            registry = self.registry(root)
            staged = self.evidence(root / "staged")
            evidence_root = root / "evidence" / staged.manifest["evidence_hash"]
            evidence_root.parent.mkdir(parents=True)
            staged.root.replace(evidence_root)
            evidence = load_frozen_evidence(evidence_root)
            result = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            ).run(
                evidence=evidence, variants=[self.main_variant(registry)],
                profile=self.profile(), secret="secret", caller=FakeModel(),
            )
            call_path = next((Path(result["batch_root"]) / "model_calls").glob("*.json"))
            call = json.loads(call_path.read_text())
            call["prompt"] += "\nTAMPERED"
            call_path.write_text(json.dumps(call), encoding="utf-8")
            dashboard = ResearchDashboardIndex(
                batches_root=root / "batches", database=root / "index.sqlite3"
            )
            with self.assertRaises(ResearchDashboardError):
                dashboard.batch_detail(result["status"]["batch_id"])

    def test_dashboard_rejects_tampered_skill_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "research"
            registry = self.registry(root)
            staged = self.evidence(root / "staged")
            evidence_root = root / "evidence" / staged.manifest["evidence_hash"]
            evidence_root.parent.mkdir(parents=True)
            staged.root.replace(evidence_root)
            evidence = load_frozen_evidence(evidence_root)
            result = ResearchBatchRunner(
                batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=self.policy(),
            ).run(
                evidence=evidence, variants=[self.main_variant(registry)],
                profile=self.profile(), secret="secret", caller=FakeModel(),
            )
            skill_path = next((Path(result["batch_root"]) / "skills").glob("*.json"))
            skill = json.loads(skill_path.read_text())
            first_path = next(iter(skill["documents"]))
            skill["documents"][first_path] += "\nTAMPERED"
            skill_path.write_text(json.dumps(skill), encoding="utf-8")
            dashboard = ResearchDashboardIndex(
                batches_root=root / "batches", database=root / "index.sqlite3"
            )
            with self.assertRaises(ResearchDashboardError):
                dashboard.batch_detail(result["status"]["batch_id"])


if __name__ == "__main__":
    unittest.main()
