from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.collectors import CollectorError, build_capital_document, build_derivatives_document
from shaq_daily_oracle.contracts import ContractError, validate_domain_report
from shaq_daily_oracle.events import EventCaptureError, capture_sec_universe_events
from shaq_daily_oracle.deep_capture import _number, _subscribe_us_all_sessions
from shaq_daily_oracle.lineage import build_lineage_graph
from shaq_daily_oracle.sandboxed_codex import _bind_verified_lineage, derive_predictions
from shaq_daily_oracle.tasks import TaskError, build_blind_domain_tasks


class EvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cutoff = "2026-08-26T08:50:00-04:00"

    def tearDown(self):
        self.temp.cleanup()

    def raw_record(self, evidence_id, domain, name, parents=(), consumers=None, component=None):
        path = self.root / name
        if not path.exists():
            path.write_text(json.dumps({"name": name}), encoding="utf-8")
        row = {
            "evidence_id": evidence_id, "domain": domain, "provider": "test",
            "source_uri": f"https://example.test/{name}", "raw_file_path": name,
            "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "captured_at": "2026-08-26T08:40:00-04:00",
            "scope_symbols": ["AAA"],
        }
        if parents:
            row["parent_evidence_ids"] = list(parents)
        if consumers:
            row["consumer_domains"] = list(consumers)
        if component:
            row["root_component_type"] = component
        return row

    def test_canonical_sec_identity_is_mandatory(self):
        universe = self.root / "universe.csv"
        universe.write_text("instrument,cik_company_id\nAAA,123\n", encoding="utf-8")
        config = json.loads((ROOT / "config/event-discovery.json").read_text())
        analysis_config = json.loads((ROOT / "config/event-analysis.json").read_text())
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(EventCaptureError):
            capture_sec_universe_events(
                universe_csv=universe, evidence_root=self.root,
                output_manifest=self.root / "manifest.json", status_output=self.root / "status.json",
                cutoff_et=self.cutoff, previous_close_et="2026-08-25T16:00:00-04:00",
                config=config,
                analysis_config=analysis_config,
            )

    def test_previous_day_ticks_are_rejected(self):
        books = [{"observed_at_et": "2026-08-26T08:40:00-04:00", "bid": [{"price": 10, "volume": 10}], "ask": [{"price": 11, "volume": 10}]}]
        with self.assertRaises(CollectorError):
            build_capital_document(
                symbol="AAA",
                ticker_rows=[{"time": "2026-08-25 15:59:00", "price": 10, "volume": 10, "ticker_direction": "BUY"}],
                order_book_samples=books, captured_at_et="2026-08-26T08:45:00-04:00",
                window_start_et="2026-08-26T04:00:00-04:00", window_end_et=self.cutoff,
            )

    def test_ticker_subscription_uses_all_us_sessions(self):
        class Enum:
            TICKER = "ticker"
            ORDER_BOOK = "book"
            ALL = "all"

        class Quote:
            def subscribe(self, codes, subtypes, **kwargs):
                self.call = (codes, subtypes, kwargs)
                return 0, None

        quote = Quote()
        _subscribe_us_all_sessions(quote, "US.AAA", Enum, Enum)
        self.assertEqual(quote.call[2]["session"], "all")
        self.assertFalse(quote.call[2]["is_first_push"])

    def test_post_cutoff_sec_feed_event_is_rejected(self):
        universe = self.root / "late-universe.csv"
        universe.write_text("instrument,cik_company_id\nAAA,123\n", encoding="utf-8")
        config = json.loads((ROOT / "config/event-discovery.json").read_text())
        config["sec_forms"] = ["8-K"]
        config["sec_feed_max_pages_per_form"] = 1
        analysis_config = json.loads((ROOT / "config/event-analysis.json").read_text())
        feed = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><updated>2026-08-26T13:00:00Z</updated><link href="https://www.sec.gov/Archives/edgar/data/123/0000000123-26-000001-index.html"/></entry></feed>'''
        with patch.dict(os.environ, {"DAILY_ORACLE_SEC_USER_AGENT": "Research test@example.edu"}), patch(
            "shaq_daily_oracle.events._fetch", return_value=feed
        ):
            manifest, _ = capture_sec_universe_events(
                universe_csv=universe, evidence_root=self.root,
                output_manifest=self.root / "late-manifest.json",
                status_output=self.root / "late-status.json",
                cutoff_et=self.cutoff, previous_close_et="2026-08-25T16:00:00-04:00",
                config=config, analysis_config=analysis_config,
            )
        self.assertEqual(manifest["evidence"], [])

    def test_transient_sec_failure_is_retried_under_governed_policy(self):
        universe = self.root / "retry-universe.csv"
        universe.write_text("instrument,cik_company_id\nAAA,123\n", encoding="utf-8")
        config = json.loads((ROOT / "config/event-discovery.json").read_text())
        config["sec_forms"] = ["8-K"]
        config["sec_feed_max_pages_per_form"] = 1
        analysis_config = json.loads((ROOT / "config/event-analysis.json").read_text())
        empty_feed = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'''
        with patch.dict(os.environ, {"DAILY_ORACLE_SEC_USER_AGENT": "Research test@example.edu"}), patch(
            "shaq_daily_oracle.events._fetch", side_effect=[URLError("temporary"), empty_feed]
        ) as fetch, patch("shaq_daily_oracle.events.time.sleep"):
            _, status = capture_sec_universe_events(
                universe_csv=universe, evidence_root=self.root,
                output_manifest=self.root / "retry-manifest.json",
                status_output=self.root / "retry-status.json",
                cutoff_et=self.cutoff, previous_close_et="2026-08-25T16:00:00-04:00",
                config=config, analysis_config=analysis_config,
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(status["statuses"], [])

    def test_option_direction_requires_single_leg_and_oi_confirmation(self):
        self.assertIsNone(_number("N/A"))
        chain = [
            {"code": "C", "option_type": "CALL", "strike_time": "2026-09-18", "strike_price": 100, "bid_price": 4, "ask_price": 5, "option_implied_volatility": 30},
            {"code": "P", "option_type": "PUT", "strike_time": "2026-09-18", "strike_price": 100, "bid_price": 3, "ask_price": 4, "option_implied_volatility": 35},
        ]
        events = [
            {"option_code": "C", "ticker_type": "BUY", "option_type": "CALL", "order_type_list": ["SWEEP"], "strategy_type": "N/A", "turnover": 100000},
            {"option_code": "P", "ticker_type": "BUY", "option_type": "PUT", "order_type_list": ["MULTI_LEG"], "strategy_type": "VERTICAL"},
        ]
        unconfirmed = build_derivatives_document(symbol="AAA", underlying_price=100, option_rows=chain, option_events=events, captured_at_et=self.cutoff)
        confirmed = build_derivatives_document(symbol="AAA", underlying_price=100, option_rows=chain, option_events=events, oi_increase_confirmations={"C": True}, captured_at_et=self.cutoff)
        self.assertFalse(unconfirmed["directional_flow_eligible"])
        self.assertTrue(confirmed["directional_flow_eligible"])
        self.assertEqual(confirmed["confirmed_direction"], "bullish")
        self.assertEqual(len(confirmed["option_event_rows"]), 1)

    def test_prov_multi_parent_keeps_two_roots_without_merging(self):
        records = [
            self.raw_record("stock", "price_volume", "stock.json", component="stock_price_volume"),
            self.raw_record("sector", "market", "sector.json", component="industry_context"),
            self.raw_record("derived", "relationships", "derived.json", parents=("stock", "sector")),
        ]
        graph = build_lineage_graph(records, self.root, self.cutoff)
        self.assertEqual(graph["independent_root_count"], 2)
        self.assertEqual(len(graph["evidence_to_roots"]["derived"]), 2)
        self.assertNotIn("derived", graph["evidence_to_root"])

    def test_same_event_across_domains_is_one_root(self):
        shared = self.root / "shared.json"
        shared.write_text('{"event":"earnings"}', encoding="utf-8")
        sha = hashlib.sha256(shared.read_bytes()).hexdigest()
        base = {"provider": "issuer", "source_uri": "https://issuer.test/event", "raw_file_path": shared.name, "raw_sha256": sha, "captured_at": "2026-08-26T08:40:00-04:00", "upstream_event_id": "sec:1"}
        graph = build_lineage_graph([
            {**base, "evidence_id": "event", "domain": "event"},
            {**base, "evidence_id": "spillover", "domain": "relationships"},
        ], self.root, self.cutoff)
        self.assertEqual(graph["independent_root_count"], 1)

    def test_program_derives_lineage_roots_from_cited_evidence(self):
        report = {"evidence_ids": ["b", "a"], "lineage_root_ids": ["model-invented-root"]}
        bound = _bind_verified_lineage(report, {"a": ["r2", "r1"], "b": ["r1", "r3"]})
        self.assertEqual(bound["lineage_root_ids"], ["r1", "r2", "r3"])
        with self.assertRaises(Exception):
            _bind_verified_lineage({"evidence_ids": ["missing"]}, {"a": ["r1"]})

    def test_cross_domain_raw_routing_keeps_answers_blind(self):
        records = [self.raw_record(
            "price", "price_volume", "price.json",
            consumers=("price_volume", "event", "capital"), component="stock_price_volume",
        )]
        graph = build_lineage_graph(records, self.root, self.cutoff)
        tasks = build_blind_domain_tasks(lineage=graph, symbols=["AAA"], as_of_et=self.cutoff, horizon="official_US_regular_session_open_to_close")
        event_task = next(row for row in tasks["tasks"] if row["domain"] == "event")
        self.assertEqual([row["evidence_id"] for row in event_task["evidence"]], ["price"])
        self.assertEqual(event_task["collection_status"]["status"], "not_applicable")
        (self.root / "price.json").write_text('{"label":"bullish"}', encoding="utf-8")
        records[0]["raw_sha256"] = hashlib.sha256((self.root / "price.json").read_bytes()).hexdigest()
        graph = build_lineage_graph(records, self.root, self.cutoff)
        with self.assertRaises(TaskError):
            build_blind_domain_tasks(lineage=graph, symbols=["AAA"], as_of_et=self.cutoff, horizon="official_US_regular_session_open_to_close")

    def test_availability_semantics_are_fail_closed(self):
        roots = {"e": ["r"]}
        consumers = {"e": {"event"}}
        base = {"domain": "event", "as_of_et": self.cutoff, "horizon": "official_US_regular_session_open_to_close", "component_type": "company_event", "thesis": "No event today.", "antithesis": "A late event would change the state.", "unknowns": [], "invalidation": [], "evidence_ids": [], "lineage_root_ids": []}
        validate_domain_report({**base, "availability": "available", "verdict": "not_applicable"}, roots, consumers)
        validate_domain_report({**base, "availability": "available", "verdict": "neutral"}, roots, consumers)
        with self.assertRaises(ContractError):
            validate_domain_report({**base, "availability": "provider_error", "verdict": "neutral"}, roots, consumers)

    def test_repaired_gate_requires_context_and_stock_root(self):
        # Keep construction explicit so six-domain report order cannot affect the result.
        reports = {"AAA": [
            {"domain": "market", "verdict": "bullish", "lineage_root_ids": ["m"]},
            {"domain": "price_volume", "verdict": "bullish", "lineage_root_ids": ["s"]},
            *[{"domain": name, "verdict": "neutral", "lineage_root_ids": []} for name in ("relationships", "event", "capital", "derivatives")],
        ]}
        policy = {
            "minimum_aligned_independent_roots": 2, "minimum_aligned_applicable_domains": 2,
            "maximum_opposed_independent_roots": 0, "maximum_predictions": 3,
            "market_or_industry_root_component_types": ["market_context", "industry_context"],
            "stock_specific_root_component_types": ["stock_price_volume"],
            "root_component_types": {"m": ["market_context"], "s": ["stock_price_volume"]},
        }
        result = derive_predictions(
            reports_by_symbol=reports,
            adversary_by_symbol={"AAA": {"veto": False}},
            candidate_intake={"candidates": [{"symbol": "AAA", "gics_sector": "Industrials"}]},
            integration_policy=policy,
        )
        self.assertEqual(result[0]["direction"], "bullish")
        policy["root_component_types"] = {"m": ["market_context"], "s": ["market_context"]}
        self.assertEqual(derive_predictions(reports_by_symbol=reports, adversary_by_symbol={"AAA": {"veto": False}}, candidate_intake={"candidates": [{"symbol": "AAA", "gics_sector": "Industrials"}]}, integration_policy=policy), [])


if __name__ == "__main__":
    unittest.main()
