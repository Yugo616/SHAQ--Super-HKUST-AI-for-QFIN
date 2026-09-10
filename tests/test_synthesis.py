import copy
import unittest

from shaq_daily_oracle import decision_sandbox as sandbox


class SynthesisTests(unittest.TestCase):
    def packet(self):
        return sandbox.build_decision_input(
            as_of_et="2026-09-09T07:00:00-04:00",
            candidate_intake={"candidates": [{"symbol": "EXAMPLE"}]},
            reports_by_symbol={"EXAMPLE": [
                {"domain": "market", "availability": "available", "verdict": "neutral", "lineage_root_ids": ["market"]},
                {"domain": "event", "availability": "available", "verdict": "bearish", "lineage_root_ids": ["event"]},
            ]},
            adversary_by_symbol={"EXAMPLE": {"veto": False}},
            root_component_types={"market": ["market_context"], "event": ["stock_event"]},
        )

    def test_synthesis_does_not_require_directional_domain_votes(self):
        packet = self.packet()
        script = 'function decide(i) { return {schema_version:1, decisions:[{symbol:"EXAMPLE", action:"publish", direction:"bullish", reason:"经营改善可能超过共同压力", evidence_root_ids:["event"]}]}; }'
        result = sandbox.execute_decision_script(script=script, decision_input=packet, citation_policy="available")
        self.assertEqual(result["predictions"][0]["direction"], "bullish")
        with self.assertRaises(sandbox.DecisionSandboxError):
            sandbox.execute_decision_script(script=script, decision_input=packet)

    def test_synthesis_keeps_integrity_and_reference_checks(self):
        script = 'function decide(i) { return {schema_version:1, decisions:[{symbol:"EXAMPLE", action:"publish", direction:"bullish", reason:"判断", evidence_root_ids:["event"]}]}; }'
        for mutation in ("veto", "missing", "unavailable", "future_label"):
            packet = self.packet()
            event = next(r for r in packet["reports_by_symbol"]["EXAMPLE"] if r["domain"] == "event")
            if mutation == "veto":
                packet["adversary_by_symbol"]["EXAMPLE"]["veto"] = True
            elif mutation == "missing":
                event["lineage_root_ids"] = []
            elif mutation == "unavailable":
                event["availability"] = "no_data"
            else:
                packet["next_return"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(sandbox.DecisionSandboxError):
                sandbox.execute_decision_script(script=script, decision_input=packet, citation_policy="available")

    def test_synthesis_validates_evidence_per_candidate_and_required_reasons(self):
        from shaq_daily_oracle.synthesis import validate_synthesis
        reports = {"EXAMPLE": [{"availability": "available", "evidence_ids": ["e1"]}]}
        value = {"decisions": [{"symbol": "EXAMPLE", "action": "publish", "direction": "bullish",
            "thesis": "经营改善", "antithesis": "市场下行", "resolution": "公司因素预计占优",
            "comparison": "唯一候选", "unknowns": ["吸收程度"], "invalidation": ["经营改善不能传导"], "evidence_ids": ["e1"]}]}
        result = validate_synthesis(value, reports, {"e1": ["root"]}, maximum_predictions=3)
        self.assertEqual(result["decisions"][0]["evidence_root_ids"], ["root"])
        for key, replacement in (("evidence_ids", ["invented"]), ("resolution", ""), ("comparison", "")):
            bad = copy.deepcopy(value)
            bad["decisions"][0][key] = replacement
            with self.assertRaises(ValueError):
                validate_synthesis(bad, reports, {"e1": ["root"]}, maximum_predictions=3)

    def test_synthesis_empty_board_and_cap(self):
        from shaq_daily_oracle.synthesis import validate_synthesis
        value = {"decisions": [{"symbol": "X", "action": "reject", "direction": "neutral",
            "thesis": "无依据", "antithesis": "无依据", "resolution": "缺数据", "comparison": "无可比依据",
            "unknowns": [], "invalidation": [], "evidence_ids": []}]}
        self.assertEqual(validate_synthesis(value, {"X": []}, {}, maximum_predictions=3)["decisions"][0]["action"], "reject")
        value["decisions"][0].update(action="publish", direction="bullish", evidence_ids=["e"])
        with self.assertRaises(ValueError):
            validate_synthesis(value, {"X": [{"availability": "available", "evidence_ids": ["e"]}]}, {"e": ["r"]}, maximum_predictions=0)

    def test_opt_in_batch_records_synthesis_without_changing_main(self):
        import json
        import tempfile
        from pathlib import Path
        from test_research_batch import ResearchBatchTests, FakeModel
        from shaq_daily_oracle.research_batch import ResearchBatchRunner, VariantSelection
        from shaq_daily_oracle.bundled_versions import install_bundled_versions

        class Model(FakeModel):
            def __call__(self, **kw):
                if "FROZEN SYNTHESIS INPUT:" not in kw["prompt"]:
                    return super().__call__(**kw)
                value = {"decisions": [{"symbol": "AAPL", "action": "publish", "direction": "bullish",
                    "thesis": "个股支持", "antithesis": "可能吸收", "resolution": "个股机制占优",
                    "comparison": "唯一候选", "unknowns": [], "invalidation": ["机制失效"],
                    "evidence_ids": ["ev_price_aapl"]}]}
                import hashlib
                from shaq_daily_oracle.hashing import sha256_payload
                return value, {"profile_sha256": kw["profile"].identity(),
                    "prompt_sha256": hashlib.sha256(kw["prompt"].encode()).hexdigest(),
                    "schema_sha256": sha256_payload(kw["schema"]), "output_sha256": sha256_payload(value)}

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fixtures = ResearchBatchTests()
            registry = fixtures.registry(root)
            install_bundled_versions(registry)
            variants = [
                VariantSelection.from_registry_row(row)
                for row in registry.list_method_versions()
            ]
            result = ResearchBatchRunner(batches_root=root / "batches", cache_root=root / "cache",
                registry=registry, integration_policy=fixtures.policy()).run(
                    evidence=fixtures.evidence(root / "evidence-staging"), variants=variants, profile=fixtures.profile(), secret="", caller=Model())
            stored = list((Path(result["batch_root"]) / "variants").glob("*/variant_result.json"))
            rows = [json.loads(p.read_text()) for p in stored]
            shadow = next(r for r in rows if r["variant"]["version_id"] == "cross-domain-synthesis-1")
            self.assertEqual(shadow["synthesis"]["decisions"][0]["resolution"], "个股机制占优")
            self.assertEqual(shadow["predictions"][0]["symbol"], "AAPL")
            self.assertEqual(shadow["orders"], [])
            baseline = next(r for r in rows if r["variant"]["version_id"] == "independent-gate-1")
            self.assertNotIn("synthesis", baseline)
            self.assertEqual(shadow["evidence_hash"], baseline["evidence_hash"])
            self.assertEqual(shadow["candidate_set_sha256"], baseline["candidate_set_sha256"])
            from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
            detail = ResearchDashboardIndex(batches_root=root / "batches", database=root / "index.sqlite3").batch_detail(Path(result["batch_root"]).name)
            embedded = json.loads(
                (Path(__file__).parents[1] / "bundled_versions/cross-domain-synthesis.json").read_text()
            )
            self.assertEqual(
                detail["skill_snapshots"]["team/cross-domain-synthesis-1"]["documents"]["decision/decision.js"],
                embedded["files"]["decision/decision.js"],
            )
            from shaq_daily_oracle.virtual_accounts import AccountStore, AccountRules
            AccountStore(root / 'virtual_accounts').activate(AccountRules())
            index = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3')
            overview = index.overview()
            self.assertIn('virtual_accounts', overview)
            self.assertEqual(len(overview['virtual_accounts']['results']), 2)
            self.assertTrue(all(r['scope'] in ('historical', 'practice', 'late') for r in overview['virtual_accounts']['results']))
            (root / 'index.sqlite3').unlink()
            rebuilt = ResearchDashboardIndex(batches_root=root / 'batches', database=root / 'index.sqlite3').overview()
            self.assertEqual(overview['virtual_accounts'], rebuilt['virtual_accounts'])


if __name__ == "__main__":
    unittest.main()
