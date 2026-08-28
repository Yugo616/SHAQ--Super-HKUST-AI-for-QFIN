from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.hashing import sha256_payload  # noqa: E402
from shaq_daily_oracle.batch_review import adwin_mean_shift, build_batch_review  # noqa: E402
from shaq_daily_oracle.identity import (  # noqa: E402
    IdentityError,
    ensure_formal_core_lock,
    formal_core_lock_path,
    formal_core_sha256,
    resolve_system_identity,
)
from shaq_daily_oracle.postmortem import (  # noqa: E402
    PostmortemError,
    build_outcome_document,
    build_postmortem,
    validate_postmortem,
)
from shaq_daily_oracle.tasks import build_blind_domain_tasks  # noqa: E402
from shaq_daily_oracle.postmortem_runner import _postmortem_schema  # noqa: E402


class PostmortemTests(unittest.TestCase):
    def frozen(self) -> dict:
        reports = []
        for domain in (
            "market", "relationships", "event", "capital", "derivatives", "price_volume"
        ):
            reports.append({
                "domain": domain,
                "as_of_et": "2026-08-26T08:50:00-04:00",
                "horizon": "official_US_regular_session_open_to_close",
                "availability": "available",
                "verdict": "neutral",
                "component_type": "market_beta" if domain == "market" else "price_volume_state",
                "thesis": "frozen",
                "antithesis": "frozen",
                "unknowns": [],
                "invalidation": [],
                "evidence_ids": [],
                "lineage_root_ids": [],
            })
        value = {
            "schema_version": 6,
            "run_id": "SHAQ-CANARY-2026-08-26-001",
            "created_at": "2026-08-26T08:55:00-04:00",
            "cutoff_et": "2026-08-26T08:50:00-04:00",
            "publication_deadline_et": "2026-08-26T09:00:00-04:00",
            "mode": "canary",
            "formal_ai_enabled": True,
            "formal_eligibility": True,
            "system_identity": "SHAQ-EVIDENCE-PROV-2026-08-26",
            "system_config_sha256": "a" * 64,
            "candidate_intake_sha256": "b" * 64,
            "integration_policy_sha256": "c" * 64,
            "isolation_status_sha256": "d" * 64,
            "prediction_target": "official_unadjusted_US_regular_session_open_to_close",
            "probability_publication_allowed": False,
            "p_committee_hit": None,
            "p_net_profit": None,
            "predictions": [],
            "reports_by_symbol": {"AAA": reports},
            "adversary_by_symbol": {"AAA": {"veto": False}},
            "integration_audit_by_symbol": {"AAA": {
                "published": False,
                "applicable_domain_count": 6,
                "directional_domain_count": 0,
                "directional_domains": [],
                "rejection_reasons": ["fewer_than_two_directional_domains"],
            }},
            "lineage": {},
        }
        value["run_sha256"] = sha256_payload(value)
        return value

    def outcomes(self, frozen: dict, *, phase: str = "provisional") -> dict:
        captured = (
            "2026-08-27T08:10:00-04:00"
            if phase == "final" else "2026-08-26T16:05:00-04:00"
        )
        bars = {
            "AAA": [{
                "time_key": "2026-08-26", "open": 100, "high": 104,
                "low": 99, "close": 103, "volume": 1000,
            }],
            "SPY": [{
                "time_key": "2026-08-26", "open": 500, "high": 506,
                "low": 499, "close": 505, "volume": 100000,
            }],
            "XLK": [{
                "time_key": "2026-08-26", "open": 200, "high": 204,
                "low": 199, "close": 204, "volume": 50000,
            }],
        }
        return build_outcome_document(
            run_id=frozen["run_id"], frozen_run_sha256=frozen["run_sha256"],
            trade_date="2026-08-26", phase=phase, captured_at_et=captured,
            session_close_et="2026-08-26T16:00:00-04:00", rows_by_symbol=bars,
        )

    def postmortem(self, *, phase: str = "provisional") -> dict:
        frozen = self.frozen()
        return build_postmortem(
            frozen=frozen,
            candidate_intake={"candidates": [{
                "symbol": "AAA", "gics_sector": "Information Technology",
                "sector_benchmark": "XLK",
            }]},
            relationships_by_symbol={"AAA": {
                "symbol": "AAA", "sector_benchmark": "XLK", "sector_beta": 0.8,
                "multi_etf_beta_126": {"SPY": 1.2, "XLK": 1.1},
            }},
            outcomes=self.outcomes(frozen, phase=phase),
            generated_at_et=(
                "2026-08-27T08:11:00-04:00"
                if phase == "final" else "2026-08-26T16:06:00-04:00"
            ),
            approved_reference_ids={"REF-DAWID-001"},
        )

    def test_empty_forecast_still_reviews_every_frozen_candidate(self):
        result = self.postmortem()
        self.assertEqual(result["candidate_count"], 1)
        row = result["candidate_diagnostics"][0]
        self.assertFalse(row["published"])
        self.assertTrue(row["uncovered_realized_move"])
        self.assertEqual(len(row["domain_diagnostics"]), 6)
        self.assertTrue(all(
            item["diagnostic"] == "neutral_with_realized_component"
            for item in row["domain_diagnostics"]
        ))

    def test_decomposition_uses_only_frozen_exposures_and_closes_exactly(self):
        result = self.postmortem()
        attribution = result["candidate_diagnostics"][0]["attribution"]
        self.assertEqual(attribution["market_beta_126_frozen"], 1.2)
        self.assertEqual(attribution["sector_beta_126_frozen"], 0.8)
        self.assertAlmostEqual(attribution["decomposition_identity_error"], 0.0)
        self.assertEqual(
            attribution["estimation_policy"],
            "T_minus_1_frozen_exposures_no_same_day_refit",
        )

    def test_final_reobservation_requires_a_later_date(self):
        frozen = self.frozen()
        bars = {symbol: [{
            "time_key": "2026-08-26", "open": 100, "high": 101,
            "low": 99, "close": 100, "volume": 1,
        }] for symbol in ("AAA", "SPY", "XLK")}
        with self.assertRaises(PostmortemError):
            build_outcome_document(
                run_id=frozen["run_id"], frozen_run_sha256=frozen["run_sha256"],
                trade_date="2026-08-26", phase="final",
                captured_at_et="2026-08-26T16:10:00-04:00",
                session_close_et="2026-08-26T16:00:00-04:00",
                rows_by_symbol=bars,
            )
        self.assertEqual(self.postmortem(phase="final")["phase"], "final")

    def test_ai_explanation_is_hypothesis_only_and_cannot_mutate_production(self):
        frozen = self.frozen()
        hypothesis = {
            "hypothesis_id": "HYP-001",
            "symbol": "AAA",
            "diagnostic_category": "post_cutoff_shock",
            "economic_mechanism": "A new filing may have changed cash-flow expectations.",
            "affected_domains": ["event"],
            "expected_improvement": "Detect cutoff-to-close primary events in retrospectives.",
            "invalidation_conditions": ["The filing contains no new facts."],
            "reference_ids": ["REF-DAWID-001"],
            "source_ids": ["sec:one"],
            "alternative_explanations": ["Market-wide repricing."],
            "strongest_countercase": "Timing alone does not establish causality.",
        }
        result = build_postmortem(
            frozen=frozen,
            candidate_intake={"candidates": [{
                "symbol": "AAA", "gics_sector": "Information Technology",
                "sector_benchmark": "XLK",
            }]},
            relationships_by_symbol={"AAA": {
                "symbol": "AAA", "sector_benchmark": "XLK", "sector_beta": 0.8,
                "multi_etf_beta_126": {"SPY": 1.2},
            }},
            outcomes=self.outcomes(frozen),
            generated_at_et="2026-08-26T16:06:00-04:00",
            approved_reference_ids={"REF-DAWID-001"},
            post_cutoff_sources=[{
                "source_id": "sec:one",
                "symbol": "AAA",
                "published_at_et": "2026-08-26T12:00:00-04:00",
                "source_uri": "https://www.sec.gov/example",
                "raw_sha256": "f" * 64,
            }],
            ai_hypotheses=[hypothesis],
        )
        saved = result["learning_hypotheses"][0]
        self.assertEqual(saved["causal_claim_policy"], "hypothesis_not_fact")
        self.assertFalse(saved["automatic_mutation_allowed"])
        self.assertFalse(result["automatic_production_mutation_allowed"])
        validate_postmortem(result, frozen)

        duplicated = {**hypothesis, "reference_ids": ["REF-DAWID-001", "REF-DAWID-001"]}
        with self.assertRaises(PostmortemError):
            build_postmortem(
                frozen=frozen,
                candidate_intake={"candidates": [{
                    "symbol": "AAA", "gics_sector": "Information Technology",
                    "sector_benchmark": "XLK",
                }]},
                relationships_by_symbol={"AAA": {
                    "symbol": "AAA", "sector_benchmark": "XLK", "sector_beta": 0.8,
                    "multi_etf_beta_126": {"SPY": 1.2},
                }},
                outcomes=self.outcomes(frozen), generated_at_et="2026-08-26T16:06:00-04:00",
                approved_reference_ids={"REF-DAWID-001"},
                post_cutoff_sources=[{
                    "source_id": "sec:one", "symbol": "AAA",
                    "published_at_et": "2026-08-26T12:00:00-04:00",
                    "source_uri": "https://www.sec.gov/example", "raw_sha256": "f" * 64,
                }], ai_hypotheses=[duplicated],
            )

    def test_postmortem_api_schema_uses_supported_subset(self):
        self.assertNotIn("uniqueItems", json.dumps(_postmortem_schema(), sort_keys=True))

    def test_postmortem_directory_is_not_a_prediction_evidence_source(self):
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            (runtime / "postmortem").mkdir()
            (runtime / "postmortem" / "label-decoy.json").write_text(
                json.dumps({"next_return": 1, "winner": True}), encoding="utf-8"
            )
            tasks = build_blind_domain_tasks(
                lineage={
                    "records": [], "evidence_to_roots": {},
                    "root_component_types": {}, "roots": [],
                },
                symbols=["AAA"],
                as_of_et="2026-08-27T08:50:00-04:00",
                horizon="official_US_regular_session_open_to_close",
            )
            serialized = json.dumps(tasks, sort_keys=True)
            self.assertNotIn("next_return", serialized)
            self.assertNotIn("label-decoy", serialized)

    def test_formal_core_lock_ignores_postmortem_files_but_rejects_core_drift(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "governance").mkdir()
            (root / "src").mkdir()
            (root / "governance/formal-core-manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "include_patterns": ["src/formal.py"],
            }), encoding="utf-8")
            (root / "src/formal.py").write_text("VALUE = 1\n", encoding="utf-8")
            first = formal_core_sha256(root)
            (root / "src/postmortem.py").write_text("RESULT = 1\n", encoding="utf-8")
            self.assertEqual(first, formal_core_sha256(root))
            runtime = root / "runtime"
            ensure_formal_core_lock(
                package_root=root, runtime_root=runtime, system_identity="identity",
                freeze_start=datetime.fromisoformat("2026-08-26").date(),
                freeze_end=datetime.fromisoformat("2026-09-04").date(),
                observed_at=datetime.fromisoformat("2026-08-26T08:00:00-04:00"),
            )
            (root / "src/formal.py").write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaises(IdentityError):
                ensure_formal_core_lock(
                    package_root=root, runtime_root=runtime, system_identity="identity",
                    freeze_start=datetime.fromisoformat("2026-08-26").date(),
                    freeze_end=datetime.fromisoformat("2026-09-04").date(),
                    observed_at=datetime.fromisoformat("2026-08-27T08:00:00-04:00"),
                )

    def test_identity_history_starts_a_new_lock_without_overwriting_the_old_one(self):
        config = {
            "identities": [
                {"identity": "old", "effective_from_et": "2026-08-26T00:00:00-04:00"},
                {"identity": "new", "effective_from_et": "2026-08-31T00:00:00-04:00"},
            ]
        }
        old_time = datetime.fromisoformat("2026-08-28T08:00:00-04:00")
        new_time = datetime.fromisoformat("2026-08-31T08:00:00-04:00")
        self.assertEqual(resolve_system_identity(config, old_time)["identity"], "old")
        self.assertEqual(resolve_system_identity(config, new_time)["identity"], "new")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "governance").mkdir()
            (root / "src").mkdir()
            (root / "governance/formal-core-manifest.json").write_text(json.dumps({
                "schema_version": 1, "include_patterns": ["src/formal.py"],
            }), encoding="utf-8")
            (root / "src/formal.py").write_text("VALUE = 1\n", encoding="utf-8")
            runtime = root / "runtime"
            old_lock = ensure_formal_core_lock(
                package_root=root, runtime_root=runtime, system_identity="old",
                freeze_start=old_time.date(), freeze_end=old_time.date(), observed_at=old_time,
            )
            old_bytes = (runtime / "formal_core_lock.json").read_bytes()
            new_lock = ensure_formal_core_lock(
                package_root=root, runtime_root=runtime, system_identity="new",
                freeze_start=new_time.date(), freeze_end=new_time.date(), observed_at=new_time,
            )
            self.assertEqual((runtime / "formal_core_lock.json").read_bytes(), old_bytes)
            self.assertEqual(old_lock["system_identity"], "old")
            self.assertEqual(new_lock["system_identity"], "new")
            self.assertTrue(formal_core_lock_path(runtime, "new").is_file())

    def test_batch_review_clusters_repetition_without_mutating_core(self):
        documents = []
        for day, symbol in (("2026-08-26", "AAA"), ("2026-08-27", "BBB")):
            unsigned = {
                "schema_version": 1,
                "run_id": f"run-{day}",
                "phase": "final",
                "trade_date": day,
                "candidate_diagnostics": [{
                    "symbol": symbol,
                    "published": False,
                    "published_correct": None,
                    "uncovered_realized_move": True,
                    "domain_diagnostics": [{
                        "domain": "event", "diagnostic": "evidence_missing"
                    }],
                }],
                "learning_hypotheses": [{
                    "symbol": symbol,
                    "diagnostic_category": "evidence_missing",
                    "affected_domains": ["event"],
                    "reference_ids": ["REF-DAWID-PREQUENTIAL-001"],
                }],
                "automatic_production_mutation_allowed": False,
                "prediction_runtime_read_allowed": False,
            }
            documents.append({**unsigned, "postmortem_sha256": sha256_payload(unsigned)})
        review = build_batch_review(
            postmortems=documents,
            generated_at_et="2026-09-05T08:30:00-04:00",
            review_interval_sessions=20,
            drift_delta=0.002,
            minimum_promotion_days=120,
            minimum_promotion_forecasts=300,
        )
        self.assertEqual(review["repeated_research_hypotheses"][0]["occurrences"], 2)
        self.assertFalse(review["formal_core_mutation_performed"])
        self.assertFalse(review["promotion_gate"]["promotion_allowed"])

    def test_drift_detection_is_alert_only_and_deterministic(self):
        stable = adwin_mean_shift([0.1] * 40, delta=0.002)
        changed = adwin_mean_shift([0.0] * 50 + [1.0] * 50, delta=0.002)
        self.assertFalse(stable["alert"])
        self.assertTrue(changed["alert"])
        self.assertEqual(changed, adwin_mean_shift([0.0] * 50 + [1.0] * 50, delta=0.002))


if __name__ == "__main__":
    unittest.main()
