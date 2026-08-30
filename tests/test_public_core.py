from __future__ import annotations

import hashlib
import csv
import json
import subprocess
import sys
import tempfile
import unittest
import jsonschema
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import (  # noqa: E402
    CandidateError,
    CanaryError,
    ContractError,
    EvidenceError,
    apply_broker_update,
    audit_runtime,
    broker_update_from_row,
    broker_remark,
    build_canary_intents,
    build_capture_receipt,
    build_seatbelt_profile,
    build_sec_analysis_text,
    build_sec_view_receipt,
    sec_document_types,
    build_blind_domain_tasks,
    build_lineage_graph,
    build_prospective_evaluations,
    build_symbol_snapshot_documents,
    build_snapshot_evidence_manifest,
    build_primary_event_record,
    build_price_history_document,
    build_price_history_analysis,
    build_label_row,
    build_no_ai_run_input,
    evaluation_record,
    enforce_execution_window,
    execution_cost_components,
    extract_sec_acceptance_proof,
    exit_quantity_from_entry,
    find_broker_order,
    formal_ai_status,
    register_intent,
    cost_model,
    derive_effective_universe,
    derive_predictions,
    probability_readiness,
    resolve_premarket_return,
    freeze_run,
    validate_adversary_report,
    validate_domain_report,
    validate_label_capture_time,
    validate_public_https_url,
    verify_execution_bundle,
    verify_isolation_attestation,
    select_simulate_us_account,
    select_candidates,
    normalize_futu_order_status,
    net_profit_readiness,
    merge_evidence_manifests,
    reconciled_journal_status,
)
from shaq_daily_oracle.hashing import sha256_payload  # noqa: E402
from shaq_daily_oracle.sandboxed_codex import _report_schema  # noqa: E402


class PublicCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "event.json"
        self.raw.write_text('{"event":"earnings"}', encoding="utf-8")
        self.sha = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.cutoff = "2026-08-20T08:55:00-04:00"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, evidence_id: str, domain: str, **changes) -> dict:
        value = {
            "evidence_id": evidence_id,
            "domain": domain,
            "provider": "issuer",
            "source_uri": "https://issuer.example/release",
            "raw_file_path": self.raw.name,
            "raw_sha256": self.sha,
            "captured_at": "2026-08-20T08:30:00-04:00",
            "published_at": "2026-08-20T07:00:00-04:00",
        }
        value.update(changes)
        return value

    def graph(self) -> dict:
        return build_lineage_graph([self.record("e1", "event")], self.root, self.cutoff)

    def report(self) -> dict:
        graph = self.graph()
        return {
            "domain": "event",
            "as_of_et": self.cutoff,
            "horizon": "official_US_regular_session_open_to_close",
            "verdict": "bullish",
            "thesis": "Forward guidance changed expected cash flow.",
            "antithesis": "The premarket move may already absorb the news.",
            "unknowns": ["opening auction supply"],
            "invalidation": ["primary guidance is withdrawn"],
            "evidence_ids": ["e1"],
            "lineage_root_ids": [graph["evidence_to_root"]["e1"]],
        }

    def policy(self, created_at: str = "2026-08-20T08:30:00-04:00") -> dict:
        return {
            "run_id": "SHAQ-CANARY-001",
            "created_at": created_at,
            "intent_created_at": created_at,
            "portfolio_observed_at": created_at,
            "borrow_captured_at": created_at,
            "forecast_cutoff": self.cutoff,
            "entry_after": "2026-08-20T09:30:00-04:00",
            "entry_deadline": "2026-08-20T09:35:00-04:00",
            "exit_at": "2026-08-20T15:55:00-04:00",
            "exit_deadline": "2026-08-20T15:58:00-04:00",
            "trd_env": "SIMULATE",
            "real_trading_enabled": False,
            "account_allowlist": ["US_SIMULATE_CANARY"],
            "max_forecasts": 3,
            "shares_per_forecast": 1,
            "max_portfolio_age_seconds": 300,
            "max_borrow_age_seconds": 300,
        }

    def integration_policy(self) -> dict:
        return {
            "minimum_aligned_independent_roots": 2,
            "maximum_opposed_independent_roots": 0,
            "maximum_predictions": 3,
            "required_aligned_domain_groups": [
                ["market", "relationships", "event"],
                ["capital", "derivatives", "price_volume"],
            ],
            "parameter_bindings": {
                "minimum_aligned_independent_roots": ["REF-FINCON-001", "DEC-INTEGRATION-001", "EXP-CANARY-001"],
                "maximum_opposed_independent_roots": ["REF-FINCON-001", "DEC-INTEGRATION-001", "EXP-CANARY-001"],
                "maximum_predictions": ["REF-FINCON-001", "DEC-CAP-001", "EXP-CANARY-001"],
                "required_aligned_domain_groups": ["REF-FINCON-001", "DEC-INTEGRATION-001", "EXP-CANARY-001"],
            },
        }

    def isolation_status(self, enabled: bool = True) -> dict:
        return {
            "schema_version": 6,
            "backend": "test_enforced" if enabled else "unavailable",
            "formal_ai_enabled": enabled,
            "evidence_read_only": enabled,
            "labels_unmounted": enabled,
            "network_denied": enabled,
            "tools_denied": enabled,
            "reason": "test capability fixture" if enabled else "not isolated",
        }

    def test_same_source_across_domains_is_one_root(self):
        graph = build_lineage_graph(
            [self.record("event", "event"), self.record("capital", "capital")],
            self.root,
            self.cutoff,
        )
        self.assertEqual(graph["independent_root_count"], 1)

    def test_premarket_return_never_uses_last_or_stale_prev_close(self):
        row = {
            "last_price": 13.80,
            "prev_close_price": 12.90,
            "pre_price": 13.20,
            "pre_change_val": -0.19,
            "pre_change_rate": -1.419,
        }
        first = resolve_premarket_return(row, tolerance=0.0005)
        self.assertEqual(first["status"], "pass")
        self.assertAlmostEqual(first["premarket_return"], 13.20 / 13.39 - 1.0)
        self.assertEqual(first["snapshot_prev_close_status"], "rejected_mismatch")
        row["last_price"] = 99.0
        row["prev_close_price"] = 99.0
        second = resolve_premarket_return(row, tolerance=0.0005)
        self.assertEqual(first["premarket_return"], second["premarket_return"])

    def test_batched_snapshot_is_split_into_independent_symbol_files(self):
        documents = build_symbol_snapshot_documents(
            provider="Futu OpenD",
            captured_at_start_et="2026-08-20T08:30:00-04:00",
            captured_at_end_et="2026-08-20T08:31:00-04:00",
            cutoff_et=self.cutoff,
            rows=[
                {"symbol": "AAPL", "raw_snapshot": {"pre_price": 200.0}},
                {"symbol": "MSFT", "raw_snapshot": {"pre_price": 500.0}},
            ],
        )
        records = []
        for index, (symbol, document) in enumerate(documents.items()):
            path = self.root / f"{symbol}.json"
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            records.append(self.record(
                f"snapshot-{index}",
                "price_volume",
                raw_file_path=path.name,
                raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                source_uri=f"futu-opend://market-snapshot/US.{symbol}",
                scope_symbols=[symbol],
            ))
        graph = build_lineage_graph(records, self.root, self.cutoff)
        self.assertEqual(graph["independent_root_count"], 2)
        with self.assertRaises(Exception):
            build_symbol_snapshot_documents(
                provider="Futu OpenD",
                captured_at_start_et="2026-08-20T08:30:00-04:00",
                captured_at_end_et="2026-08-20T08:31:00-04:00",
                cutoff_et=self.cutoff,
                rows=[{"symbol": "../AAPL"}],
            )

    def test_snapshot_manifest_rehashes_symbol_files_and_scopes_domains(self):
        evidence_root = self.root / "evidence"
        stocks_dir = evidence_root / "stocks"
        benchmarks_dir = evidence_root / "benchmarks"
        stocks_dir.mkdir(parents=True)
        benchmarks_dir.mkdir(parents=True)
        captured = "2026-08-20T08:30:00-04:00"

        def make_snapshot(symbol, directory):
            document = {
                "symbol": symbol,
                "provider_symbol": f"US.{symbol}",
                "captured_at_end_et": captured,
            }
            path = directory / f"{symbol}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return ({
                "formal_cutoff_eligible": True,
                "provider": "Futu OpenD",
                "captured_at_end_et": captured,
                "cutoff_et": self.cutoff,
                "rows": [{"symbol": symbol}],
                "symbol_files": {
                    symbol: {
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                },
            }, path)

        stocks, stock_path = make_snapshot("AAPL", stocks_dir)
        benchmarks, _ = make_snapshot("SPY", benchmarks_dir)
        manifest = build_snapshot_evidence_manifest(
            stock_snapshot=stocks,
            benchmark_snapshot=benchmarks,
            evidence_root=evidence_root,
            benchmark_channels={"SPY": "equity"},
        )
        by_domain = {row["domain"]: row for row in manifest["evidence"]}
        self.assertEqual(by_domain["price_volume"]["scope_symbols"], ["AAPL"])
        self.assertEqual(by_domain["market"]["scope_symbols"], ["*"])
        raw_event = evidence_root / "event.html"
        proof_span = '"datePublished":"2026-08-20T07:00:00-04:00"'
        raw_event.write_text(
            f"<SEC-DOCUMENT>{proof_span}\n"
            "<DOCUMENT>\n<TYPE>8-K\n<FILENAME>form8k.htm\n<TEXT><html><body>Cover fact</body></html></TEXT>\n</DOCUMENT>\n"
            "<DOCUMENT>\n<TYPE>EX-99.1\n<FILENAME>release.htm\n<TEXT><html><body><p>Revenue fact</p>"
            "<ix:hidden>hidden duplicate</ix:hidden></body></html></TEXT>\n</DOCUMENT>\n"
            "<DOCUMENT>\n<TYPE>GRAPHIC\n<FILENAME>image.jpg\n<TEXT>binary secret</TEXT>\n</DOCUMENT>\n"
            "</SEC-DOCUMENT>",
            encoding="utf-8",
        )
        analysis = build_sec_analysis_text(
            raw_event.read_bytes(),
            document_types=["8-K", "EX-99.1"],
            maximum_output_bytes=10000,
        )
        self.assertIn(b"Revenue fact", analysis)
        self.assertNotIn(b"binary secret", analysis)
        self.assertNotIn(b"hidden duplicate", analysis)
        self.assertEqual(sec_document_types(raw_event.read_bytes()), ("8-K", "EX-99.1", "GRAPHIC"))
        only_present = build_sec_analysis_text(
            raw_event.read_bytes(), document_types=["8-K"], maximum_output_bytes=10000,
        )
        self.assertIn(b"Cover fact", only_present)
        analysis_path = evidence_root / "event.analysis.txt"
        analysis_path.write_bytes(analysis)
        analysis_receipt = build_sec_view_receipt(
            raw=raw_event.read_bytes(), analysis=analysis,
            document_types=["8-K", "EX-99.1"], maximum_output_bytes=10000,
        )
        analysis_receipt_path = evidence_root / "event.analysis.receipt.json"
        analysis_receipt_path.write_text(json.dumps(analysis_receipt), encoding="utf-8")
        receipt_event = evidence_root / "event.receipt.json"
        receipt = build_capture_receipt(
            source_uri="https://issuer.example/release",
            final_uri="https://issuer.example/release",
            published_at="2026-08-20T07:00:00-04:00",
            captured_at_start_et="2026-08-20T08:29:00-04:00",
            captured_at_end_et=captured,
            cutoff_et=self.cutoff,
            status_code=200,
            content_type="text/html",
            content_length=len(raw_event.read_bytes()),
            raw_sha256=hashlib.sha256(raw_event.read_bytes()).hexdigest(),
            publication_proof={
                "method": "embedded_date_published",
                "raw_span": proof_span,
            },
        )
        receipt_event.write_text(json.dumps(receipt), encoding="utf-8")
        event = build_primary_event_record(
            symbol="AAPL", raw_file=raw_event, receipt_file=receipt_event,
            evidence_root=evidence_root, analysis_file=analysis_path,
            analysis_receipt_file=analysis_receipt_path,
        )
        merged = merge_evidence_manifests(manifest, [event])
        self.assertEqual(len(merged["evidence"]), 3)
        self.assertEqual(event["domain"], "event")
        self.assertEqual(event["scope_symbols"], ["AAPL"])
        self.assertEqual(event["analysis_transform"]["name"], "sec_document_text_view_v1")
        self.assertEqual(
            build_lineage_graph([event], evidence_root, self.cutoff)["independent_root_count"], 1
        )
        analysis_path.write_text("forged summary", encoding="utf-8")
        forged = dict(event, analysis_sha256=hashlib.sha256(analysis_path.read_bytes()).hexdigest())
        with self.assertRaises(EvidenceError):
            build_lineage_graph([forged], evidence_root, self.cutoff)
        analysis_path.write_bytes(analysis)
        bars = [
            {"time_key": "2026-08-18 00:00:00", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
            {"time_key": "2026-08-19 00:00:00", "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1200},
        ]
        path_document = build_price_history_document(
            symbol="AAPL", sector_benchmark="XLK",
            premarket_context={
                "stock_premarket_return": 0.02, "sector_premarket_return": 0.01,
                "residual_premarket_return": 0.01, "premarket_volume": 5000,
            },
            stock_bars=bars, sector_bars=bars, end_date="2026-08-19", minimum_bars=2,
            captured_at_et=captured, cutoff_et=self.cutoff,
        )
        self.assertEqual(path_document["adjustment"], "NONE")
        raw_path = evidence_root / "price-history.json"
        raw_path.write_text(json.dumps(path_document, sort_keys=True), encoding="utf-8")
        analysis_bytes = build_price_history_analysis(raw_path.read_bytes(), maximum_bars=2)
        analysis_path = evidence_root / "price-history.analysis.json"
        analysis_path.write_bytes(analysis_bytes)
        history_record = {
            "evidence_id": "price-history-aapl",
            "domain": "price_volume",
            "provider": "Futu OpenD",
            "source_uri": "futu-opend://historical-kline/US.AAPL",
            "raw_file_path": str(raw_path),
            "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "captured_at": captured,
            "scope_symbols": ["AAPL"],
            "analysis_file_path": str(analysis_path),
            "analysis_sha256": hashlib.sha256(analysis_bytes).hexdigest(),
            "analysis_transform": {
                "name": "price_path_analysis_view_v1",
                "source_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "maximum_bars": 2,
            },
        }
        self.assertEqual(
            build_lineage_graph([history_record], evidence_root, self.cutoff)["independent_root_count"], 1
        )
        analysis_path.write_bytes(analysis_bytes.replace(b"AAPL", b"MSFT"))
        forged_history = dict(
            history_record,
            analysis_sha256=hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
        )
        with self.assertRaises(EvidenceError):
            build_lineage_graph([forged_history], evidence_root, self.cutoff)
        with self.assertRaises(Exception):
            build_price_history_document(
                symbol="AAPL", sector_benchmark="XLK",
                premarket_context=path_document["premarket_context"],
                stock_bars=bars + [dict(bars[-1], time_key="2026-08-20 00:00:00")],
                sector_bars=bars, end_date="2026-08-19", minimum_bars=2,
                captured_at_et=captured, cutoff_et=self.cutoff,
            )
        no_proof = {key: value for key, value in receipt.items() if key not in {"publication_proof", "receipt_sha256"}}
        no_proof["receipt_sha256"] = sha256_payload(no_proof)
        receipt_event.write_text(json.dumps(no_proof), encoding="utf-8")
        with self.assertRaises(Exception):
            build_primary_event_record(
                symbol="AAPL", raw_file=raw_event, receipt_file=receipt_event,
                evidence_root=evidence_root,
            )
        receipt_event.write_text(json.dumps(receipt), encoding="utf-8")
        unsafe_receipt = dict(receipt, status_code=500)
        unsigned_unsafe = {
            key: value for key, value in unsafe_receipt.items() if key != "receipt_sha256"
        }
        unsafe_receipt["receipt_sha256"] = sha256_payload(unsigned_unsafe)
        receipt_event.write_text(json.dumps(unsafe_receipt), encoding="utf-8")
        with self.assertRaises(Exception):
            build_primary_event_record(
                symbol="AAPL", raw_file=raw_event, receipt_file=receipt_event,
                evidence_root=evidence_root,
            )
        receipt_event.write_text(json.dumps(receipt), encoding="utf-8")
        wrong_length_receipt = build_capture_receipt(
            source_uri=receipt["source_uri"], final_uri=receipt["final_uri"],
            published_at=receipt["published_at"],
            captured_at_start_et=receipt["captured_at_start_et"],
            captured_at_end_et=receipt["captured_at_end_et"],
            cutoff_et=receipt["cutoff_et"], status_code=receipt["status_code"],
            content_type=receipt["content_type"],
            content_length=receipt["content_length"] + 1,
            raw_sha256=receipt["raw_sha256"],
        )
        receipt_event.write_text(json.dumps(wrong_length_receipt), encoding="utf-8")
        with self.assertRaises(Exception):
            build_primary_event_record(
                symbol="AAPL", raw_file=raw_event, receipt_file=receipt_event,
                evidence_root=evidence_root,
            )
        receipt_event.write_text(json.dumps(receipt), encoding="utf-8")
        stock_path.write_text('{"tampered":true}', encoding="utf-8")
        with self.assertRaises(Exception):
            build_snapshot_evidence_manifest(
                stock_snapshot=stocks,
                benchmark_snapshot=benchmarks,
                evidence_root=evidence_root,
                benchmark_channels={"SPY": "equity"},
            )

    def test_timestamp_hash_or_orphan_fails_closed(self):
        with self.assertRaises(EvidenceError):
            build_lineage_graph(
                [self.record("future", "event", captured_at="2026-08-20T08:55:01-04:00")],
                self.root,
                self.cutoff,
            )
        with self.assertRaises(EvidenceError):
            build_lineage_graph([self.record("bad", "event", raw_sha256="0" * 64)], self.root, self.cutoff)
        with self.assertRaises(EvidenceError):
            build_lineage_graph([self.record("child", "event", parent_evidence_ids=["missing"])], self.root, self.cutoff)

    def test_primary_source_capture_receipt_rejects_local_or_late_material(self):
        sec_time, sec_span = extract_sec_acceptance_proof(
            b"<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20260820074512\n</SEC-HEADER>"
        )
        self.assertEqual(sec_time, "2026-08-20T07:45:12-04:00")
        self.assertEqual(sec_span, "ACCEPTANCE-DATETIME>20260820074512")
        with self.assertRaises(Exception):
            extract_sec_acceptance_proof(b"no acceptance header")
        self.assertEqual(
            validate_public_https_url("https://www.sec.gov/Archives/test", resolve=False),
            "https://www.sec.gov/Archives/test",
        )
        for unsafe in ("http://example.com/x", "https://127.0.0.1/x", "https://user@example.com/x"):
            with self.assertRaises(Exception):
                validate_public_https_url(unsafe, resolve=False)
        valid = build_capture_receipt(
            source_uri="https://issuer.example/release",
            final_uri="https://issuer.example/release",
            published_at="2026-08-20T07:00:00-04:00",
            captured_at_start_et="2026-08-20T08:30:00-04:00",
            captured_at_end_et="2026-08-20T08:31:00-04:00",
            cutoff_et=self.cutoff,
            status_code=200,
            content_type="text/html",
            content_length=10,
            raw_sha256="a" * 64,
        )
        self.assertEqual(len(valid["receipt_sha256"]), 64)
        with self.assertRaises(Exception):
            build_capture_receipt(
                source_uri="https://issuer.example/release",
                final_uri="https://issuer.example/release",
                published_at="2026-08-20T07:00:00-04:00",
                captured_at_start_et="2026-08-20T08:54:00-04:00",
                captured_at_end_et="2026-08-20T08:55:01-04:00",
                cutoff_et=self.cutoff,
                status_code=200,
                content_type="text/html",
                content_length=10,
                raw_sha256="a" * 64,
            )
        with self.assertRaises(Exception):
            build_capture_receipt(
                source_uri="https://issuer.example/release",
                final_uri="https://issuer.example/release",
                published_at="2026-08-20T08:40:00-04:00",
                captured_at_start_et="2026-08-20T08:30:00-04:00",
                captured_at_end_et="2026-08-20T08:31:00-04:00",
                cutoff_et=self.cutoff,
                status_code=200,
                content_type="text/html",
                content_length=10,
                raw_sha256="a" * 64,
            )

    def test_effective_universe_uses_verified_official_event(self):
        base = self.root / "base.csv"
        base.write_text(
            "ticker,company_name,gics_sector,gics_sub_industry\n"
            "EA,Electronic Arts,Communication Services,Entertainment\n"
            "AAPL,Apple,Information Technology,Hardware\n",
            encoding="utf-8",
        )
        source = self.root / "official.html"
        source.write_text(
            "Ferguson Enterprises (FERG) will replace Electronic Arts (EA) in the S&P 500 "
            "effective prior to the open of trading on Wednesday, August 5, 2026.",
            encoding="utf-8",
        )
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        receipt = build_capture_receipt(
            source_uri="https://press.example/change",
            final_uri="https://press.example/change",
            published_at="2026-07-31T23:59:59-04:00",
            captured_at_start_et="2026-08-20T08:30:00-04:00",
            captured_at_end_et="2026-08-20T08:31:00-04:00",
            cutoff_et=self.cutoff,
            status_code=200,
            content_type="text/html",
            content_length=len(source.read_bytes()),
            raw_sha256=source_sha,
        )
        receipt_path = self.root / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        events = [
            {
                "event_id": "official-add-ferg",
                "effective_date": "2026-08-05",
                "action": "addition",
                "ticker": "FERG",
                "company_name": "Ferguson Enterprises",
                "gics_sector": "Industrials",
                "gics_sub_industry": "Trading Companies & Distributors",
                "source_assertions": ["Ferguson Enterprises", "FERG", "August 5, 2026"],
            },
            {
                "event_id": "official-delete-ea",
                "effective_date": "2026-08-05",
                "action": "deletion",
                "ticker": "EA",
                "source_assertions": ["Electronic Arts", "EA", "August 5, 2026"],
            },
        ]
        rows, manifest = derive_effective_universe(
            base_csv=base,
            source_path=source,
            receipt_path=receipt_path,
            events=events,
            as_of="2026-08-20",
        )
        symbols = {row["instrument"] for row in rows}
        self.assertEqual(symbols, {"AAPL", "FERG"})
        self.assertEqual(manifest["base_count"], manifest["effective_count"])
        ferg = next(row for row in rows if row["instrument"] == "FERG")
        self.assertEqual(ferg["known_from_utc"], "2026-08-20T12:31:00Z")
        source.write_text("tampered", encoding="utf-8")
        with self.assertRaises(Exception):
            derive_effective_universe(
                base_csv=base,
                source_path=source,
                receipt_path=receipt_path,
                events=events,
                as_of="2026-08-20",
            )

    def test_cycle_fails_closed(self):
        with self.assertRaises(EvidenceError):
            build_lineage_graph(
                [
                    self.record("a", "event", parent_evidence_ids=["b"]),
                    self.record("b", "event", parent_evidence_ids=["a"]),
                ],
                self.root,
                self.cutoff,
            )

    def test_domain_report_is_strict_and_blind(self):
        graph = self.graph()
        domains = {record["evidence_id"]: record["domain"] for record in graph["records"]}
        self.assertEqual(
            validate_domain_report(self.report(), graph["evidence_to_root"], domains)["verdict"],
            "bullish",
        )
        leaked = self.report()
        leaked["confidence"] = 0.9
        with self.assertRaises(ContractError):
            validate_domain_report(leaked, graph["evidence_to_root"], domains)
        unsupported = self.report()
        unsupported["evidence_ids"] = []
        unsupported["lineage_root_ids"] = []
        with self.assertRaises(ContractError):
            validate_domain_report(unsupported, graph["evidence_to_root"], domains)
        wrong_domain = self.report()
        wrong_domain["domain"] = "price_volume"
        with self.assertRaises(ContractError):
            validate_domain_report(wrong_domain, graph["evidence_to_root"], domains)
        abstention = self.report()
        abstention["verdict"] = "unavailable"
        abstention["thesis"] = "The cited packet lacks the event-level semantics required for direction."
        self.assertEqual(
            validate_domain_report(
                abstention, graph["evidence_to_root"], domains
            )["verdict"],
            "unavailable",
        )

    def test_structured_output_schema_forbids_availability_verdict_conflicts(self):
        report = {
            **self.report(),
            "availability": "available",
            "component_type": "company_event",
        }
        generated = _report_schema()["properties"]["results"]["items"]["properties"]["report"]
        public = json.loads(
            (ROOT / "schemas/domain-report.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.validate(report, generated)
        jsonschema.validate(report, public)
        for availability, verdict in (
            ("available", "unavailable"),
            ("no_data", "neutral"),
        ):
            invalid = {**report, "availability": availability, "verdict": verdict}
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, generated)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, public)

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt profile")
    def test_sandbox_profile_and_deterministic_integrator_fail_closed(self):
        profile = build_seatbelt_profile(
            codex_path=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            workspace_root=self.root,
            temp_root=self.root.parent / "isolated-output",
        )
        self.assertIn(f'(deny file-read* (subpath "{self.root.resolve()}"))', profile)
        self.assertIn('(allow process-exec (literal "/Applications/ChatGPT.app/Contents/Resources/codex"))', profile)
        self.assertNotIn('(allow process-exec (literal "/bin/sh"))', profile)

        def report(domain, verdict, roots):
            return {
                "domain": domain,
                "as_of_et": self.cutoff,
                "horizon": "official_US_regular_session_open_to_close",
                "verdict": verdict,
                "thesis": "Mechanism is supported by the frozen packet.",
                "antithesis": "The open may already absorb the mechanism.",
                "unknowns": [],
                "invalidation": [],
                "evidence_ids": [],
                "lineage_root_ids": roots,
            }

        reports = [
            report("market", "neutral", []),
            report("relationships", "neutral", []),
            report("event", "bullish", ["root-event"]),
            report("capital", "neutral", []),
            report("derivatives", "neutral", []),
            report("price_volume", "bullish", ["root-price"]),
        ]
        adversary = {
            "AAPL": {
                "counts_as_vote": False, "new_evidence_allowed": False,
                "duplicate_lineage_roots": [], "unresolved_conflicts": [],
                "strongest_countercase": "The gap may be fully absorbed.",
                "veto": False, "veto_reason": "",
            }
        }
        intake = {"candidates": [{"symbol": "AAPL", "gics_sector": "Information Technology"}]}
        first = derive_predictions(
            reports_by_symbol={"AAPL": reports}, adversary_by_symbol=adversary,
            candidate_intake=intake, integration_policy=self.integration_policy(),
        )
        second = derive_predictions(
            reports_by_symbol={"AAPL": list(reversed(reports))}, adversary_by_symbol=adversary,
            candidate_intake=intake, integration_policy=self.integration_policy(),
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0]["direction"], "bullish")
        same_root = json.loads(json.dumps(reports))
        next(row for row in same_root if row["domain"] == "price_volume")["lineage_root_ids"] = ["root-event"]
        self.assertEqual(
            derive_predictions(
                reports_by_symbol={"AAPL": same_root}, adversary_by_symbol=adversary,
                candidate_intake=intake, integration_policy=self.integration_policy(),
            ),
            [],
        )

        status = self.isolation_status()
        unsigned = {
            "schema_version": 6,
            "started_at_et": "2026-08-20T08:00:00-04:00",
            "completed_at_et": "2026-08-20T08:01:00-04:00",
            "workspace_root": str(self.root.resolve()),
            "backend": status["backend"],
            "codex_cli_version": "test",
            "production_profile_sha256": "a" * 64,
            "checks": {"workspace_denied": True},
            "status": status,
        }
        artifact = {**unsigned, "attestation_sha256": sha256_payload(unsigned)}
        attestation = self.root / "attestation.json"
        attestation.write_text(json.dumps(artifact), encoding="utf-8")
        verify_isolation_attestation(
            status=status, attestation_path=attestation, workspace_root=self.root
        )
        artifact["checks"]["workspace_denied"] = False
        attestation.write_text(json.dumps(artifact), encoding="utf-8")
        with self.assertRaises(Exception):
            verify_isolation_attestation(
                status=status, attestation_path=attestation, workspace_root=self.root
            )

    def test_blind_task_builder_partitions_domains_and_rejects_answer_fields(self):
        graph = build_lineage_graph(
            [
                self.record("event", "event", scope_symbols=["AAPL"]),
                self.record("market", "market", scope_symbols=["*"]),
            ],
            self.root,
            self.cutoff,
        )
        first = build_blind_domain_tasks(
            lineage=graph,
            symbols=["AAPL"],
            as_of_et=self.cutoff,
            horizon="official_US_regular_session_open_to_close",
        )
        second = build_blind_domain_tasks(
            lineage={**graph, "records": list(reversed(graph["records"]))},
            symbols=["AAPL"],
            as_of_et=self.cutoff,
            horizon="official_US_regular_session_open_to_close",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first["tasks"]), 6)
        for task in first["tasks"]:
            self.assertTrue(all(record["evidence_id"] == task["domain"] for record in task["evidence"]))
            self.assertNotIn("core_direction", json.dumps(task))

        contaminated = self.root / "contaminated.json"
        contaminated.write_text('{"direction":"bullish"}', encoding="utf-8")
        bad_sha = hashlib.sha256(contaminated.read_bytes()).hexdigest()
        bad_graph = build_lineage_graph(
            [self.record("bad", "event", raw_file_path=contaminated.name, raw_sha256=bad_sha)],
            self.root,
            self.cutoff,
        )
        with self.assertRaises(Exception):
            build_blind_domain_tasks(
                lineage=bad_graph,
                symbols=["AAPL"],
                as_of_et=self.cutoff,
                horizon="official_US_regular_session_open_to_close",
            )

    def test_report_order_and_duplicate_family_names_do_not_change_lineage(self):
        a = self.record("a", "event", independent_family="one")
        b = self.record("b", "capital", independent_family="two")
        left = build_lineage_graph([a, b], self.root, self.cutoff)
        right = build_lineage_graph([dict(b, independent_family="x"), dict(a, independent_family="y")], self.root, self.cutoff)
        self.assertEqual(left["clusters"], right["clusters"])
        channel_graph = build_lineage_graph(
            [
                self.record("spy", "market", upstream_event_id="market-channel:equity:cutoff"),
                self.record("qqq", "market", upstream_event_id="market-channel:equity:cutoff"),
            ],
            self.root,
            self.cutoff,
        )
        self.assertEqual(channel_graph["independent_root_count"], 1)

    def test_adversary_never_votes_or_adds_evidence(self):
        value = {
            "counts_as_vote": False,
            "new_evidence_allowed": False,
            "duplicate_lineage_roots": [],
            "unresolved_conflicts": [],
            "strongest_countercase": "The gap may be fully absorbed.",
            "veto": False,
            "veto_reason": "",
        }
        self.assertFalse(validate_adversary_report(value)["counts_as_vote"])
        with self.assertRaises(ContractError):
            validate_adversary_report(dict(value, counts_as_vote=True))

    def test_canary_caps_size_and_excludes_external_positions(self):
        output = build_canary_intents(
            forecasts=[{"symbol": "AAPL", "direction": "bullish", "score_eligible": True}],
            portfolio={
                "account_alias": "US_SIMULATE_CANARY",
                "trd_env": "SIMULATE",
                "positions": [{"symbol": "NVDA", "quantity": 10, "origin": "external"}],
            },
            borrowable={},
            policy=self.policy(),
        )
        self.assertEqual(output["mode"], "canary")
        self.assertEqual(output["external_positions"], ["NVDA"])
        self.assertEqual(output["intents"][0]["quantity"], 1)
        self.assertIsNone(output["p_committee_hit"])
        enforce_execution_window(
            datetime.fromisoformat("2026-08-20T15:56:00-04:00"), self.policy(), "exit"
        )
        with self.assertRaises(Exception):
            enforce_execution_window(
                datetime.fromisoformat("2026-08-20T15:59:00-04:00"), self.policy(), "exit"
            )
        malformed_schedule = self.policy()
        malformed_schedule["exit_deadline"] = "2026-08-20T15:54:00-04:00"
        with self.assertRaises(CanaryError):
            build_canary_intents(
                forecasts=[{"symbol": "AAPL", "direction": "bullish", "score_eligible": True}],
                portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE", "positions": []},
                borrowable={},
                policy=malformed_schedule,
            )

        isolated = build_canary_intents(
            forecasts=[{"symbol": "NVDA", "direction": "bullish", "score_eligible": True}],
            portfolio={
                "account_alias": "US_SIMULATE_CANARY",
                "trd_env": "SIMULATE",
                "positions": [{"symbol": "NVDA", "quantity": 10, "origin": "external"}],
            },
            borrowable={},
            policy=self.policy(),
        )
        self.assertEqual(isolated["intents"], [])
        forged_origin = build_canary_intents(
            forecasts=[{"symbol": "NVDA", "direction": "bullish", "score_eligible": True}],
            portfolio={
                "account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE",
                "positions": [{"symbol": "NVDA", "quantity": 10, "origin": "shaq"}],
            },
            borrowable={}, policy=self.policy(),
        )
        self.assertEqual(forged_origin["intents"], [])

    def test_machine_canary_audit_covers_zero_trade_and_detects_tampering(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        universe_dir = runtime / "universe"
        universe_dir.mkdir()
        base_universe = universe_dir / "base.csv"
        source_universe = universe_dir / "official.html"
        events_universe = universe_dir / "events.json"
        output_universe = universe_dir / "effective.csv"
        receipt_universe = universe_dir / "receipt.json"
        base_universe.write_text("ticker\nAAPL\n", encoding="utf-8")
        source_universe.write_text("official index event", encoding="utf-8")
        events_universe.write_text('{"events":[]}', encoding="utf-8")
        output_universe.write_text(
            "instrument,gics_sector\nAAPL,Information Technology\n", encoding="utf-8"
        )
        receipt_value = {
            "raw_sha256": hashlib.sha256(source_universe.read_bytes()).hexdigest(),
            "source_uri": "https://press.example/index",
        }
        receipt_value["receipt_sha256"] = sha256_payload(receipt_value)
        receipt_universe.write_text(json.dumps(receipt_value), encoding="utf-8")
        universe_manifest = {
            "base_path": str(base_universe),
            "base_sha256": hashlib.sha256(base_universe.read_bytes()).hexdigest(),
            "source_path": str(source_universe),
            "source_sha256": hashlib.sha256(source_universe.read_bytes()).hexdigest(),
            "source_receipt_path": str(receipt_universe),
            "source_receipt_file_sha256": hashlib.sha256(receipt_universe.read_bytes()).hexdigest(),
            "events_path": str(events_universe),
            "events_sha256": hashlib.sha256(events_universe.read_bytes()).hexdigest(),
            "output_path": str(output_universe),
            "output_sha256": hashlib.sha256(output_universe.read_bytes()).hexdigest(),
            "effective_count": 1,
            "derivation_sha256": "a" * 64,
        }
        (universe_dir / "active_manifest.json").write_text(
            json.dumps(universe_manifest), encoding="utf-8"
        )
        frozen_raw = runtime / "raw.json"
        frozen_raw.write_text('{"fact":"cutoff-safe"}', encoding="utf-8")
        frozen_raw_sha = hashlib.sha256(frozen_raw.read_bytes()).hexdigest()
        integration_policy = self.integration_policy()
        (runtime / "integration_policy.json").write_text(
            json.dumps(integration_policy), encoding="utf-8"
        )
        candidate_policy = {
            "maximum_price_residual_candidates": 6,
            "maximum_captured_event_candidates": 4,
            "minimum_premarket_volume_quantile": 0.5,
            "maximum_snapshot_skew_seconds": 120,
            "parameter_bindings": {
                "maximum_price_residual_candidates": ["REF-CHARTING-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "maximum_captured_event_candidates": ["REF-PIT-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "minimum_premarket_volume_quantile": ["REF-CHARTING-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "maximum_snapshot_skew_seconds": ["REF-FUTU-QUOTE-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
            },
        }
        (runtime / "candidate_policy.json").write_text(
            json.dumps(candidate_policy), encoding="utf-8"
        )
        benchmark_config = runtime / "benchmark_config.csv"
        benchmark_config.write_text(
            "instrument,gics_sector\nXLK,Information Technology\n", encoding="utf-8"
        )
        stock_snapshot_path = runtime / "stock_snapshot.json"
        benchmark_snapshot_path = runtime / "benchmark_snapshot.json"
        stock_snapshot = {
            "formal_cutoff_eligible": True,
            "captured_at_end_et": "2026-08-20T08:30:00-04:00",
            "universe": {"sha256": hashlib.sha256(output_universe.read_bytes()).hexdigest()},
            "rows": [],
        }
        benchmark_snapshot = {
            "formal_cutoff_eligible": True,
            "captured_at_end_et": "2026-08-20T08:30:10-04:00",
            "universe": {"sha256": hashlib.sha256(benchmark_config.read_bytes()).hexdigest()},
            "rows": [],
        }
        stock_snapshot_path.write_text(json.dumps(stock_snapshot), encoding="utf-8")
        benchmark_snapshot_path.write_text(json.dumps(benchmark_snapshot), encoding="utf-8")
        candidate_intake = select_candidates(
            stock_snapshot=stock_snapshot,
            benchmark_snapshot=benchmark_snapshot,
            universe_csv=output_universe,
            benchmark_csv=benchmark_config,
            captured_event_symbols=[],
            excluded_symbols=[],
            policy=candidate_policy,
        )
        candidate_intake["inputs"] = {
            "stock_snapshot_path": str(stock_snapshot_path),
            "stock_snapshot_sha256": hashlib.sha256(stock_snapshot_path.read_bytes()).hexdigest(),
            "benchmark_snapshot_path": str(benchmark_snapshot_path),
            "benchmark_snapshot_sha256": hashlib.sha256(benchmark_snapshot_path.read_bytes()).hexdigest(),
            "universe_path": str(output_universe),
            "universe_sha256": hashlib.sha256(output_universe.read_bytes()).hexdigest(),
            "benchmark_config_path": str(benchmark_config),
            "benchmark_config_sha256": hashlib.sha256(benchmark_config.read_bytes()).hexdigest(),
            "candidate_policy_sha256": sha256_payload(candidate_policy),
        }
        candidate_path = runtime / "candidate_intake.json"
        candidate_path.write_text(json.dumps(candidate_intake), encoding="utf-8")
        isolation_status = self.isolation_status(enabled=False)
        (runtime / "isolation_status.json").write_text(
            json.dumps(isolation_status), encoding="utf-8"
        )
        unsigned = {
            "schema_version": 6,
            "run_id": "SHAQ-CANARY-001",
            "created_at": "2026-08-20T08:30:00-04:00",
            "cutoff_et": self.cutoff,
            "mode": "canary",
            "prediction_target": "official_unadjusted_US_regular_session_open_to_close",
            "lineage": {
                "records": [{
                    "raw_file_path": str(frozen_raw),
                    "raw_sha256": frozen_raw_sha,
                }]
            },
            "reports_by_symbol": {},
            "adversary_by_symbol": {},
            "predictions": [],
            "p_committee_hit": None,
            "p_net_profit": None,
            "probability_publication_allowed": False,
            "integration_policy_sha256": sha256_payload(integration_policy),
            "candidate_intake_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            "formal_ai_enabled": False,
            "isolation_status_sha256": sha256_payload(isolation_status),
        }
        frozen = {**unsigned, "run_sha256": sha256_payload(unsigned)}
        portfolio_payload = {"trd_env": "SIMULATE"}
        borrowability_payload = {"trd_env": "SIMULATE", "borrowable": {}}
        intent_unsigned = {
            "schema_version": 6,
            "run_id": "SHAQ-CANARY-001",
            "created_at": "2026-08-20T08:31:00-04:00",
            "mode": "canary",
            "trd_env": "SIMULATE",
            "external_positions": [],
            "intents": [],
            "scientific_label": "official_unadjusted_US_regular_session_open_to_close",
            "trading_ledger": "actual_fill_to_actual_fill_separate_from_scientific_label",
            "p_committee_hit": None,
            "p_net_profit": None,
            "frozen_run_sha256": frozen["run_sha256"],
            "execution_policy_sha256": sha256_payload(self.policy()),
            "portfolio_snapshot_sha256": hashlib.sha256(
                json.dumps(portfolio_payload).encode("utf-8")
            ).hexdigest(),
            "borrowability_snapshot_sha256": hashlib.sha256(
                json.dumps(borrowability_payload).encode("utf-8")
            ).hexdigest(),
        }
        intent_bundle = {**intent_unsigned, "intent_bundle_sha256": sha256_payload(intent_unsigned)}
        artifacts = {
            "frozen_run.json": frozen,
            "portfolio_snapshot.json": portfolio_payload,
            "borrowability.json": borrowability_payload,
            "order_intents.json": intent_bundle,
            "execution_policy.json": self.policy(),
            "label_placeholder.json": {"labels": []},
            "tests_report.json": {
                "public_passed": True,
                "legacy_passed": True,
                "plugin_validator_passed": True,
                "release_validator_passed": True,
            },
            "broker_journal.json": {"run_status": "NO_TRADE", "orders": {}},
            "execution_ledger.json": {"round_trips": []},
            "portfolio_post_exit.json": {"trd_env": "SIMULATE"},
            "labels_provisional.json": {
                "schema_version": 6,
                "run_id": "SHAQ-CANARY-001",
                "provider": "Futu OpenD",
                "captured_at_et": "2026-08-20T16:05:00-04:00",
                "adjustment": "NONE",
                "session_scope": "US_regular_session",
                "official_label_status": "provisional",
                "trade_date": "2026-08-20",
                "session_close_et": "2026-08-20T16:00:00-04:00",
                "labels": [],
            },
            "evaluations_provisional.json": {
                "schema_version": 6,
                "run_id": "SHAQ-CANARY-001",
                "official_label_status": "provisional",
                "evaluations": [],
            },
            "readiness_status.json": {
                "probability": {
                    "probability_publication_allowed": False,
                    "p_committee_hit": None,
                },
                "cost": {"status": "collecting_prospective_fills"},
                "net_profit": {
                    "net_profit_publication_allowed": False,
                    "p_net_profit": None,
                },
            },
        }
        for name, value in artifacts.items():
            (runtime / name).write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(audit_runtime(runtime, "preflight")["status"], "passed")
        complete_audit = audit_runtime(runtime, "complete")
        self.assertEqual(complete_audit["status"], "passed")
        self.assertIn("labels_provisional.json", complete_audit["complete_artifact_sha256"])

        labels_path = runtime / "labels_provisional.json"
        valid_labels = json.loads(labels_path.read_text(encoding="utf-8"))
        labels_path.write_text(
            json.dumps({**valid_labels, "captured_at_et": "2026-08-20T15:59:59-04:00"}),
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            audit_runtime(runtime, "complete")
        labels_path.write_text(json.dumps(valid_labels), encoding="utf-8")

        evaluations_path = runtime / "evaluations_provisional.json"
        valid_evaluations = json.loads(evaluations_path.read_text(encoding="utf-8"))
        evaluations_path.write_text(
            json.dumps({**valid_evaluations, "official_label_status": "final"}),
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            audit_runtime(runtime, "complete")
        evaluations_path.write_text(json.dumps(valid_evaluations), encoding="utf-8")

        labels_path.write_text(json.dumps({
            **valid_labels,
            "provider": "not_applicable",
            "captured_at_et": "2026-08-20T08:55:00-04:00",
            "official_label_status": "not_applicable_no_forecasts",
        }), encoding="utf-8")
        evaluations_path.write_text(json.dumps({
            **valid_evaluations,
            "official_label_status": "not_applicable_no_forecasts",
        }), encoding="utf-8")
        self.assertEqual(audit_runtime(runtime, "complete")["status"], "passed")
        labels_path.write_text(json.dumps(valid_labels), encoding="utf-8")
        evaluations_path.write_text(json.dumps(valid_evaluations), encoding="utf-8")

        tampered_intake = dict(candidate_intake)
        tampered_intake["price_candidate_count"] = 1
        candidate_path.write_text(json.dumps(tampered_intake), encoding="utf-8")
        frozen["candidate_intake_sha256"] = hashlib.sha256(
            candidate_path.read_bytes()
        ).hexdigest()
        frozen_unsigned = dict(frozen)
        frozen_unsigned.pop("run_sha256", None)
        frozen["run_sha256"] = sha256_payload(frozen_unsigned)
        (runtime / "frozen_run.json").write_text(json.dumps(frozen), encoding="utf-8")
        with self.assertRaises(Exception):
            audit_runtime(runtime, "preflight")

        candidate_path.write_text(json.dumps(candidate_intake), encoding="utf-8")
        frozen["candidate_intake_sha256"] = hashlib.sha256(
            candidate_path.read_bytes()
        ).hexdigest()
        frozen_unsigned = dict(frozen)
        frozen_unsigned.pop("run_sha256", None)
        frozen["run_sha256"] = sha256_payload(frozen_unsigned)
        (runtime / "frozen_run.json").write_text(json.dumps(frozen), encoding="utf-8")
        frozen_raw.write_text('{"fact":"tampered"}', encoding="utf-8")
        with self.assertRaises(Exception):
            audit_runtime(runtime, "preflight")
        frozen_raw.write_text('{"fact":"cutoff-safe"}', encoding="utf-8")
        output_universe.write_text("instrument\nTAMPERED\n", encoding="utf-8")
        with self.assertRaises(Exception):
            audit_runtime(runtime, "preflight")
        output_universe.write_text("instrument\nAAPL\n", encoding="utf-8")
        frozen["mode"] = "shadow"
        (runtime / "frozen_run.json").write_text(json.dumps(frozen), encoding="utf-8")
        with self.assertRaises(Exception):
            audit_runtime(runtime, "preflight")

    def test_late_run_is_shadow_and_bear_requires_borrow(self):
        output = build_canary_intents(
            forecasts=[{"symbol": "TSLA", "direction": "bearish", "score_eligible": True}],
            portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE", "positions": []},
            borrowable={"TSLA": False},
            policy=self.policy("2026-08-20T08:55:01-04:00"),
        )
        self.assertEqual(output["mode"], "shadow")
        self.assertEqual(output["intents"], [])
        stale = self.policy()
        stale["portfolio_observed_at"] = "2026-08-20T08:20:00-04:00"
        with self.assertRaises(Exception):
            build_canary_intents(
                forecasts=[],
                portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE", "positions": []},
                borrowable={}, policy=stale,
            )

    def test_real_environment_and_duplicate_symbols_fail(self):
        policy = self.policy()
        policy["trd_env"] = "REAL"
        with self.assertRaises(CanaryError):
            build_canary_intents(forecasts=[], portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE"}, borrowable={}, policy=policy)
        forecast = {"symbol": "AAPL", "direction": "bullish", "score_eligible": True}
        with self.assertRaises(CanaryError):
            build_canary_intents(
                forecasts=[forecast, forecast],
                portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE"},
                borrowable={},
                policy=self.policy(),
            )

    def test_execution_is_idempotent_and_environment_locked(self):
        intent = {
            "intent_id": "i1",
            "idempotency_key": "key1",
            "trd_env": "SIMULATE",
        }
        journal = {}
        first = register_intent(intent, journal)
        self.assertIs(first, register_intent(intent, journal))
        with self.assertRaises(Exception):
            apply_broker_update(first, {"trd_env": "REAL", "status": "FILLED"})
        self.assertNotEqual(broker_remark("key1", "entry"), broker_remark("key1", "exit"))
        self.assertEqual(broker_remark("key1", "entry"), broker_remark("key1", "entry"))

    def test_execution_bundle_is_bound_to_frozen_direction_and_hash(self):
        output = build_canary_intents(
            forecasts=[{"symbol": "AAPL", "direction": "bullish", "score_eligible": True}],
            portfolio={"account_alias": "US_SIMULATE_CANARY", "trd_env": "SIMULATE", "positions": []},
            borrowable={}, policy=self.policy(),
        )
        frozen_unsigned = {
            "run_id": "SHAQ-CANARY-001", "mode": "canary",
            "predictions": [{"symbol": "AAPL", "direction": "bullish"}],
        }
        frozen = {**frozen_unsigned, "run_sha256": sha256_payload(frozen_unsigned)}
        output["frozen_run_sha256"] = frozen["run_sha256"]
        output["execution_policy_sha256"] = sha256_payload(self.policy())
        output["portfolio_snapshot_sha256"] = "a" * 64
        output["borrowability_snapshot_sha256"] = "b" * 64
        output["intent_bundle_sha256"] = sha256_payload(output)
        verify_execution_bundle(output, frozen, self.policy())
        output["intents"][0]["side"] = "SELL_SHORT"
        output["intent_bundle_sha256"] = sha256_payload({
            key: value for key, value in output.items() if key != "intent_bundle_sha256"
        })
        with self.assertRaises(Exception):
            verify_execution_bundle(output, frozen, self.policy())

    def test_simulate_account_resolution_never_falls_back_to_real(self):
        accounts = [
            {"acc_id": 1, "trd_env": "REAL", "trdmarket_auth": ["US"]},
            {"acc_id": 2, "trd_env": "SIMULATE", "trdmarket_auth": ["HK"]},
            {
                "acc_id": 20, "trd_env": "SIMULATE", "trdmarket_auth": ["US"],
                "acc_role": "MASTER",
            },
            {"acc_id": 3, "trd_env": "SIMULATE", "trdmarket_auth": ["US"]},
        ]
        self.assertEqual(select_simulate_us_account(accounts), 3)
        with self.assertRaises(Exception):
            select_simulate_us_account(accounts + [
                {"acc_id": 4, "trd_env": "SIMULATE", "trdmarket_auth": ["US"]}
            ])

    def test_partial_fill_and_terminal_state(self):
        record = register_intent(
            {"intent_id": "i1", "idempotency_key": "key1", "trd_env": "SIMULATE"}, {}
        )
        partial = apply_broker_update(
            record,
            {"trd_env": "SIMULATE", "status": "PARTIAL", "dealt_qty": 0.5, "dealt_avg_price": 100.0},
        )
        filled = apply_broker_update(
            partial,
            {"trd_env": "SIMULATE", "status": "FILLED", "dealt_qty": 1, "dealt_avg_price": 100.2},
        )
        self.assertEqual(filled["dealt_qty"], 1)
        with self.assertRaises(Exception):
            apply_broker_update(filled, {"trd_env": "SIMULATE", "status": "CANCELLED", "dealt_qty": 1})

    def test_futu_status_reconciliation_and_actual_exit_quantity(self):
        self.assertEqual(normalize_futu_order_status("FILLED_PART"), "PARTIAL")
        self.assertEqual(normalize_futu_order_status("CANCELLED_PART"), "CANCELLED")
        update = broker_update_from_row({
            "order_id": "42",
            "order_status": "CANCELLED_PART",
            "dealt_qty": 1,
            "dealt_avg_price": 100.25,
            "updated_time": "2026-08-20 15:54:00",
        })
        record = register_intent(
            {"intent_id": "i1", "idempotency_key": "key1", "trd_env": "SIMULATE"}, {}
        )
        reconciled = apply_broker_update(record, update)
        self.assertEqual(exit_quantity_from_entry(reconciled), 1)
        self.assertEqual(find_broker_order(reconciled, [{"order_id": "42"}])["order_id"], "42")
        with self.assertRaises(Exception):
            exit_quantity_from_entry(
                dict(reconciled, reconciliation_status="awaiting_reconciliation")
            )
        with self.assertRaises(Exception):
            broker_update_from_row({"order_id": "42", "order_status": "UNKNOWN", "dealt_qty": 0})

    def test_journal_does_not_call_an_active_order_fully_reconciled(self):
        active = {
            "one": {
                "broker_order_id": "42",
                "status": "SUBMITTED",
                "reconciliation_status": "reconciled",
            }
        }
        self.assertEqual(reconciled_journal_status(active), "RECONCILED_ACTIVE")
        active["one"]["status"] = "FILLED"
        self.assertEqual(reconciled_journal_status(active), "RECONCILED")
        active["one"] = {
            "remark": "local-reject",
            "status": "REJECTED",
            "reconciliation_status": "local_terminal",
        }
        self.assertEqual(reconciled_journal_status(active), "RECONCILED")

    def test_domain_skills_encode_required_professional_abstentions(self):
        capital = (ROOT / "skills/capital-order-flow/SKILL.md").read_text(encoding="utf-8")
        derivatives = (ROOT / "skills/derivatives-evidence/SKILL.md").read_text(encoding="utf-8")
        price_volume = (ROOT / "skills/price-volume-structure/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Return `no_data + unavailable` without a reliable order book", capital)
        self.assertIn("Do not substitute aggregate money flow", capital)
        self.assertIn("does not identify holder intent by itself", derivatives)
        self.assertIn("Max pain never enters direction", derivatives)
        self.assertIn("Do not use a universal golden/death cross", price_volume)
        self.assertIn("gap always fills", price_volume)
        registry = json.loads((ROOT / "governance/registry.json").read_text(encoding="utf-8"))
        charting = registry["references"]["REF-CHARTING-001"]
        self.assertEqual(charting["title"], "Charting by Machines")
        self.assertEqual(
            charting["url"], "https://doi.org/10.1016/j.jfineco.2024.103791"
        )
        for directory in (
            "market-common-shock", "pit-peer-spillover", "primary-event-reasoner",
            "capital-order-flow", "derivatives-evidence", "price-volume-structure",
        ):
            body = (ROOT / f"skills/{directory}/SKILL.md").read_text(encoding="utf-8")
            self.assertIn("DomainReport", body)
            self.assertLessEqual(len(body.splitlines()), 150)

    def test_market_benchmark_config_has_registered_roles_and_bindings(self):
        registry = json.loads((ROOT / "governance/registry.json").read_text(encoding="utf-8"))
        with (ROOT / "config/market-benchmarks.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), len({row["instrument"] for row in rows}))
        roles = {row["role"] for row in rows}
        self.assertTrue({
            "broad_equity", "rate_duration_proxy", "us_dollar_proxy",
            "credit_risk_proxy", "tradable_volatility_proxy",
        }.issubset(roles))
        self.assertEqual(len([role for role in roles if role.endswith("_sector")]), 11)
        for row in rows:
            self.assertIn(row["reference_id"], registry["references"])
            self.assertIn(row["decision_id"], registry["decisions"])
            self.assertIn(row["experiment_id"], registry["experiments"])

    def test_every_formal_config_parameter_has_registered_governance(self):
        registries = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / "governance").glob("*registry.json"))
        ]
        registry = {
            section: {
                key: value
                for source in registries
                for key, value in source.get(section, {}).items()
            }
            for section in ("references", "decisions", "experiments")
        }
        identity_fields = {"run_id", "parameter_bindings"}
        metadata_fields = {"reference_id", "decision_id", "experiment_id", "parameter_bindings"}
        for path in sorted((ROOT / "config").glob("*.json")):
            config = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "readiness.json":
                sections = config.values()
                excluded = metadata_fields
            else:
                sections = (config,)
                excluded = identity_fields | {"schema_version"}
            for section in sections:
                bindings = section["parameter_bindings"]
                self.assertEqual(set(bindings), set(section) - excluded, path.name)
                for reference, decision, experiment in bindings.values():
                    self.assertIn(reference, registry["references"])
                    self.assertIn(decision, registry["decisions"])
                    self.assertIn(experiment, registry["experiments"])

    def test_direction_blind_candidate_intake_is_order_invariant(self):
        universe = self.root / "universe.csv"
        universe.write_text(
            "instrument,gics_sector\nAAPL,Information Technology\nMSFT,Information Technology\n",
            encoding="utf-8",
        )
        benchmarks = self.root / "benchmarks.csv"
        benchmarks.write_text(
            "instrument,gics_sector\nXLK,Information Technology\n", encoding="utf-8"
        )
        def snapshot(rows, source, captured_at):
            return {
                "formal_cutoff_eligible": True,
                "captured_at_end_et": captured_at,
                "universe": {
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest()
                },
                "rows": [
                {
                    "symbol": symbol,
                    "premarket_semantics": {"status": "pass", "premarket_return": value},
                    "raw_snapshot": {"pre_volume": volume},
                }
                for symbol, value, volume in rows
                ],
            }
        stocks = snapshot(
            [("AAPL", 0.04, 100), ("MSFT", -0.01, 1000)],
            universe,
            "2026-08-20T08:30:00-04:00",
        )
        market = snapshot(
            [("XLK", 0.01, 5000)],
            benchmarks,
            "2026-08-20T08:30:10-04:00",
        )
        policy = {
            "maximum_price_residual_candidates": 1,
            "maximum_captured_event_candidates": 1,
            "minimum_premarket_volume_quantile": 0.5,
            "maximum_snapshot_skew_seconds": 120,
            "parameter_bindings": {
                "maximum_price_residual_candidates": ["REF-CHARTING-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "maximum_captured_event_candidates": ["REF-PIT-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "minimum_premarket_volume_quantile": ["REF-CHARTING-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
                "maximum_snapshot_skew_seconds": ["REF-FUTU-QUOTE-001", "DEC-CANDIDATE-INTAKE-001", "EXP-CANARY-001"],
            },
        }
        first = select_candidates(
            stock_snapshot=stocks, benchmark_snapshot=market,
            universe_csv=universe, benchmark_csv=benchmarks,
            captured_event_symbols=["MSFT"], excluded_symbols=[], policy=policy,
        )
        second = select_candidates(
            stock_snapshot={**stocks, "rows": list(reversed(stocks["rows"]))}, benchmark_snapshot=market,
            universe_csv=universe, benchmark_csv=benchmarks,
            captured_event_symbols=["MSFT"], excluded_symbols=[], policy=policy,
        )
        self.assertEqual(first, second)
        self.assertEqual({row["symbol"] for row in first["candidates"]}, {"AAPL", "MSFT"})
        self.assertNotIn("direction", json.dumps(first))

        misaligned = {**market, "captured_at_end_et": "2026-08-20T08:33:00-04:00"}
        with self.assertRaises(CandidateError):
            select_candidates(
                stock_snapshot=stocks, benchmark_snapshot=misaligned,
                universe_csv=universe, benchmark_csv=benchmarks,
                captured_event_symbols=[], excluded_symbols=[], policy=policy,
            )

    def test_no_ai_fallback_freezes_six_unavailable_domains_and_no_prediction(self):
        intake = {"candidates": [{"symbol": "MSFT"}, {"symbol": "AAPL"}]}
        result = build_no_ai_run_input(
            run_id="SHAQ-CANARY-001",
            created_at="2026-08-20T08:30:00-04:00",
            cutoff_et=self.cutoff,
            candidate_intake=intake,
            evidence_manifest={"evidence": [self.record("e1", "event")]},
            isolation_status=self.isolation_status(enabled=False),
        )
        self.assertEqual(result["predictions"], [])
        self.assertEqual(set(result["reports_by_symbol"]), {"AAPL", "MSFT"})
        for reports in result["reports_by_symbol"].values():
            self.assertEqual(len(reports), 6)
            self.assertTrue(all(report["verdict"] == "unavailable" for report in reports))
        self.assertTrue(all(row["veto"] for row in result["adversary_by_symbol"].values()))

    def test_scientific_and_fill_ledgers_are_separate(self):
        row = evaluation_record(
            forecast_id="f1",
            official_open=100,
            official_close=102,
            entry_fill=100.5,
            exit_fill=101.5,
            arrival_price=100.2,
            fees=0.01,
        )
        self.assertAlmostEqual(row["official_prediction_return"], 0.02)
        self.assertNotEqual(row["official_prediction_return"], row["actual_fill_return"])

        short = evaluation_record(
            forecast_id="f2",
            direction="bearish",
            official_open=100,
            official_close=98,
            entry_fill=99.5,
            exit_fill=98.5,
            arrival_price=100,
            fees=None,
        )
        self.assertTrue(short["prediction_correct"])
        self.assertGreater(short["actual_fill_return"], 0)
        self.assertGreater(short["implementation_shortfall_vs_arrival"], 0)

        flat = evaluation_record(
            forecast_id="f3", direction="bearish", official_open=100, official_close=100,
            entry_fill=None, exit_fill=None, arrival_price=None, fees=None,
        )
        self.assertFalse(flat["prediction_correct"])

        long_cost = execution_cost_components(
            direction="bullish", quantity=1, entry_fill=100.1, exit_fill=100.9,
            entry_bid=99.9, entry_ask=100.1, exit_bid=100.9, exit_ask=101.1, fees=0.02,
        )
        short_cost = execution_cost_components(
            direction="bearish", quantity=1, entry_fill=99.9, exit_fill=99.1,
            entry_bid=99.9, entry_ask=100.1, exit_bid=98.9, exit_ask=99.1, fees=0.02,
        )
        self.assertGreater(long_cost["spread_return"], 0)
        self.assertGreater(short_cost["spread_return"], 0)
        self.assertFalse(long_cost["impact_separately_identified"])

    def test_unadjusted_regular_session_label_and_flat_policy(self):
        up = build_label_row(
            run_id="SHAQ-CANARY-001", symbol="AAPL", direction="bullish", trade_date="2026-08-20",
            rows=[{"time_key": "2026-08-20 00:00:00", "open": 100, "close": 102}],
            phase="provisional", adjustment="NONE",
        )
        self.assertTrue(up["correct"])
        flat = build_label_row(
            run_id="SHAQ-CANARY-001", symbol="AAPL", direction="bearish", trade_date="2026-08-20",
            rows=[{"time_key": "2026-08-20 00:00:00", "open": 100, "close": 100}],
            phase="final", adjustment="NONE",
        )
        self.assertEqual(flat["actual_direction"], "neutral")
        self.assertFalse(flat["correct"])
        with self.assertRaises(Exception):
            build_label_row(
                run_id="SHAQ-CANARY-001", symbol="AAPL", direction="bullish", trade_date="2026-08-20",
                rows=[{"time_key": "2026-08-20", "open": 100, "close": 102}],
                phase="final", adjustment="QFQ",
            )
        with self.assertRaises(Exception):
            build_label_row(
                run_id="SHAQ-CANARY-001", symbol="AAPL", direction="bullish", trade_date="2026-08-20",
                rows=[{"time_key": "2026-08-19", "open": 100, "close": 102}],
                phase="final", adjustment="NONE",
            )
        close = datetime.fromisoformat("2026-08-20T16:00:00-04:00")
        with self.assertRaises(Exception):
            validate_label_capture_time(
                now=datetime.fromisoformat("2026-08-20T15:59:59-04:00"),
                session_close=close, trade_date="2026-08-20", phase="provisional",
            )
        validate_label_capture_time(
            now=datetime.fromisoformat("2026-08-20T16:05:00-04:00"),
            session_close=close, trade_date="2026-08-20", phase="provisional",
        )
        with self.assertRaises(Exception):
            validate_label_capture_time(
                now=datetime.fromisoformat("2026-08-20T16:05:00-04:00"),
                session_close=close, trade_date="2026-08-20", phase="final",
            )
        validate_label_capture_time(
            now=datetime.fromisoformat("2026-08-21T08:00:00-04:00"),
            session_close=close, trade_date="2026-08-20", phase="final",
        )

    def test_config_keeps_probability_null(self):
        schema = json.loads((ROOT / "schemas/forecast.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["p_committee_hit"], {"type": "null"})

    def test_probability_gate_counts_only_final_prospective_ordinary_records(self):
        rows = [
            {
                "forecast_id": "f1",
                "trade_date": "2026-08-20",
                "track": "ordinary",
                "score_eligible": True,
                "official_label_status": "final",
                "correct": True,
                "decision_signature": "sig_a",
                "p_committee_hit": None,
            },
            {
                "forecast_id": "f2",
                "trade_date": "2026-08-20",
                "track": "event",
                "score_eligible": True,
                "official_label_status": "final",
                "correct": True,
                "decision_signature": "sig_b",
                "p_committee_hit": None,
            },
        ]
        result = probability_readiness(rows, {"minimum_trading_days": 120, "minimum_evaluated_forecasts": 300})
        self.assertEqual(result["evaluated_forecasts"], 1)
        self.assertFalse(result["sample_gate_passed"])
        self.assertIsNone(result["p_committee_hit"])

    def test_prequential_probability_never_reads_same_day_or_future_outcomes(self):
        rows = [
            {
                "forecast_id": "f1", "trade_date": "2026-08-20", "track": "ordinary",
                "score_eligible": True, "official_label_status": "final", "correct": True,
                "decision_signature": "sig_a",
            },
            {
                "forecast_id": "f2", "trade_date": "2026-08-20", "track": "ordinary",
                "score_eligible": True, "official_label_status": "final", "correct": True,
                "decision_signature": "sig_a",
            },
            {
                "forecast_id": "f3", "trade_date": "2026-08-21", "track": "ordinary",
                "score_eligible": True, "official_label_status": "final", "correct": False,
                "decision_signature": "sig_a",
            },
        ]
        policy = {
            "minimum_trading_days": 2,
            "minimum_evaluated_forecasts": 3,
            "bootstrap_block_days": 1,
            "bootstrap_repetitions": 100,
            "bootstrap_seed": 7,
            "risk_coverage_fractions": [0.5, 1.0],
        }
        first = probability_readiness(rows, policy)
        repeated = probability_readiness(list(reversed(rows)), policy)
        self.assertEqual(first, repeated)
        probabilities = [
            row["research_prequential_probability"]
            for row in first["research_prequential_rows"]
        ]
        self.assertEqual(probabilities[:2], [0.5, 0.5])
        self.assertAlmostEqual(probabilities[2], 2.5 / 3.0)
        self.assertFalse(first["probability_publication_allowed"])

    def test_cost_model_unlocks_only_after_reconciled_day_and_trip_gates(self):
        component = {
            "reconciliation_status": "reconciled",
            "outcome_status": "round_trip_reconciled",
            "spread_return": 0.0001,
            "slippage_return": 0.0002,
            "fee_return": 0.0001,
            "borrow_return": 0.0,
            "impact_return": 0.0,
        }
        collecting = cost_model(
            [dict(component, trade_date="2026-08-20")],
            {"minimum_trading_days": 20, "minimum_round_trips": 20},
        )
        self.assertIsNone(collecting["estimated_cost_return"])
        rows = [dict(component, trade_date=f"2026-09-{day:02d}") for day in range(1, 21)]
        ready = cost_model(rows, {"minimum_trading_days": 20, "minimum_round_trips": 20})
        self.assertEqual(ready["status"], "operational_paper_cost_model")
        self.assertFalse(ready["real_market_impact_validated"])

    def test_gain_loss_and_net_ev_require_forward_cost_and_concentration_gates(self):
        evaluations = [
            {
                "forecast_id": "f1", "trade_date": "2026-08-20", "track": "ordinary",
                "score_eligible": True, "official_label_status": "final", "correct": True,
                "decision_signature": "sig_a", "signed_forecast_return": 0.02,
                "industry_group": "Technology",
            },
            {
                "forecast_id": "f2", "trade_date": "2026-08-21", "track": "ordinary",
                "score_eligible": True, "official_label_status": "final", "correct": False,
                "decision_signature": "sig_b", "signed_forecast_return": -0.01,
                "industry_group": "Financials",
            },
        ]
        probability = probability_readiness(
            evaluations,
            {"minimum_trading_days": 120, "minimum_evaluated_forecasts": 300},
        )
        payoff = probability["historical_payoff_distribution"]
        self.assertEqual(payoff["conditional_gain_mean"], 0.02)
        self.assertEqual(payoff["conditional_loss_mean"], 0.01)
        self.assertEqual(payoff["gain_loss_ratio"], 2.0)

        rich_evaluations = []
        trips = []
        for index, (symbol, industry, trade_date, value) in enumerate((
            ("A", "Technology", "2026-08-20", 0.01),
            ("B", "Financials", "2026-08-20", 0.02),
            ("A", "Technology", "2026-08-21", 0.01),
            ("B", "Financials", "2026-08-21", 0.02),
        )):
            forecast_id = f"net-{index}"
            rich_evaluations.append({
                "forecast_id": forecast_id, "trade_date": trade_date, "track": "ordinary",
                "score_eligible": True, "official_label_status": "final",
                "industry_group": industry,
            })
            trips.append({
                "forecast_id": forecast_id, "symbol": symbol, "trade_date": trade_date,
                "reconciliation_status": "reconciled",
                "outcome_status": "round_trip_reconciled", "net_fill_return": value,
            })
        net = net_profit_readiness(
            rich_evaluations,
            trips,
            {
                "probability_publication_allowed": True,
                "historical_payoff_distribution": {
                    "empirical_hit_rate": 0.75,
                    "conditional_gain_mean": 0.03,
                    "conditional_loss_mean": 0.01,
                },
            },
            {"status": "operational_paper_cost_model", "estimated_cost_return": 0.001},
            {"minimum_trading_days": 2, "minimum_round_trips": 4, "bootstrap_block_days": 1, "bootstrap_repetitions": 200, "bootstrap_seed": 7},
        )
        self.assertTrue(net["net_profit_publication_allowed"])
        self.assertTrue(net["concentration"]["passed"])
        self.assertAlmostEqual(net["modeled_net_expected_return"], 0.019)
        self.assertEqual(net["modeled_components"]["formula"], "p*G-(1-p)*L-cost")

        corrupted = [dict(evaluations[0], correct=None)]
        with self.assertRaises(Exception):
            probability_readiness(
                corrupted,
                {"minimum_trading_days": 120, "minimum_evaluated_forecasts": 300},
            )
        incomplete = [{
            "trade_date": "2026-08-20",
            "reconciliation_status": "reconciled",
            "outcome_status": "incomplete",
            "spread_return": 0.0001,
            "slippage_return": 0.0002,
            "fee_return": 0.0001,
            "borrow_return": 0.0,
            "impact_return": 0.0,
        }]
        ignored = cost_model(
            incomplete, {"minimum_trading_days": 1, "minimum_round_trips": 1}
        )
        self.assertEqual(ignored["round_trips"], 0)

    def test_readiness_cli_unwraps_immutable_ledger_documents(self):
        evaluations = self.root / "evaluations.json"
        trips = self.root / "trips.json"
        output = self.root / "readiness.json"
        evaluations.write_text('{"evaluations":[]}', encoding="utf-8")
        trips.write_text('{"round_trips":[]}', encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/evaluate_readiness.py"),
                "--evaluations", str(evaluations),
                "--round-trips", str(trips),
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["probability"]["probability_publication_allowed"])
        self.assertEqual(payload["cost"]["round_trips"], 0)

    def test_freeze_run_requires_six_domains_and_is_order_invariant(self):
        graph = self.graph()
        root_id = graph["evidence_to_root"]["e1"]
        reports = []
        for domain in ("market", "relationships", "event", "capital", "derivatives", "price_volume"):
            cited_ids = ["e1"] if domain == "event" else []
            cited_roots = [root_id] if domain == "event" else []
            reports.append({
                "domain": domain,
                "as_of_et": self.cutoff,
                "horizon": "official_US_regular_session_open_to_close",
                "verdict": "neutral",
                "thesis": "No verified directional increment.",
                "antithesis": "A missing observation could change the state.",
                "unknowns": [],
                "invalidation": [],
                "evidence_ids": cited_ids,
                "lineage_root_ids": cited_roots,
            })
        adversary = {
            "counts_as_vote": False,
            "new_evidence_allowed": False,
            "duplicate_lineage_roots": [root_id],
            "unresolved_conflicts": [],
            "strongest_countercase": "No directional evidence remains.",
            "veto": False,
            "veto_reason": "",
        }
        value = {
            "run_id": "SHAQ-CANARY-001",
            "created_at": "2026-08-20T08:30:00-04:00",
            "cutoff_et": self.cutoff,
            "evidence": [self.record("e1", "event")],
            "reports_by_symbol": {"AAPL": reports},
            "adversary_by_symbol": {"AAPL": adversary},
            "predictions": [],
        }
        first = freeze_run(
            run_input=value, evidence_root=self.root, integration_policy=self.integration_policy(),
            candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
        )
        value["reports_by_symbol"]["AAPL"] = list(reversed(reports))
        second = freeze_run(
            run_input=value, evidence_root=self.root, integration_policy=self.integration_policy(),
            candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
        )
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "canary")
        self.assertIsNone(first["p_committee_hit"])

        second_raw = self.root / "market.json"
        second_raw.write_text('{"market":"broad"}', encoding="utf-8")
        second_sha = hashlib.sha256(second_raw.read_bytes()).hexdigest()
        value["evidence"].append(self.record(
            "e2", "price_volume", raw_file_path=second_raw.name, raw_sha256=second_sha
        ))
        second_graph = build_lineage_graph(value["evidence"], self.root, self.cutoff)
        event_report = next(
            report for report in value["reports_by_symbol"]["AAPL"] if report["domain"] == "event"
        )
        event_report["verdict"] = "bullish"
        price_report = next(
            report for report in value["reports_by_symbol"]["AAPL"] if report["domain"] == "price_volume"
        )
        price_report["verdict"] = "bullish"
        price_report["evidence_ids"] = ["e2"]
        price_report["lineage_root_ids"] = [second_graph["evidence_to_root"]["e2"]]
        value["predictions"] = [{
            "symbol": "AAPL", "direction": "bullish", "track": "ordinary",
            "industry_group": "Information Technology", "score_eligible": True,
        }]
        published = freeze_run(
            run_input=value, evidence_root=self.root, integration_policy=self.integration_policy(),
            candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
        )
        forecast_schema = json.loads(
            (ROOT / "schemas/forecast.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(
            forecast_schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(published)
        prediction = published["predictions"][0]
        self.assertIsNone(prediction["p_committee_hit"])
        self.assertIn("decision_signature", prediction)
        self.assertEqual(prediction["strongest_countercase"], adversary["strongest_countercase"])
        self.assertEqual(prediction["decision_features"]["aligned_independent_root_count"], 2)
        self.assertFalse(formal_ai_status()["formal_ai_enabled"])
        with self.assertRaises(Exception):
            freeze_run(
                run_input=value, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64,
                isolation_status=self.isolation_status(enabled=False),
            )
        one_root = json.loads(json.dumps(value))
        one_root_graph = build_lineage_graph(one_root["evidence"], self.root, self.cutoff)
        one_root_price = next(
            report for report in one_root["reports_by_symbol"]["AAPL"]
            if report["domain"] == "price_volume"
        )
        one_root_price["evidence_ids"] = ["e1"]
        one_root_price["lineage_root_ids"] = [one_root_graph["evidence_to_root"]["e1"]]
        with self.assertRaises(Exception):
            freeze_run(
                run_input=one_root, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
            )
        opposed_raw = self.root / "opposed.json"
        opposed_raw.write_text('{"market":"opposed"}', encoding="utf-8")
        opposed_sha = hashlib.sha256(opposed_raw.read_bytes()).hexdigest()
        opposed = json.loads(json.dumps(value))
        opposed["evidence"].append(self.record(
            "e3", "market", raw_file_path=opposed_raw.name, raw_sha256=opposed_sha
        ))
        opposed_graph = build_lineage_graph(opposed["evidence"], self.root, self.cutoff)
        market_report = next(
            report for report in opposed["reports_by_symbol"]["AAPL"]
            if report["domain"] == "market"
        )
        market_report["verdict"] = "bearish"
        market_report["evidence_ids"] = ["e3"]
        market_report["lineage_root_ids"] = [opposed_graph["evidence_to_root"]["e3"]]
        with self.assertRaises(Exception):
            freeze_run(
                run_input=opposed, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
            )
        label_document = {
            "run_id": published["run_id"],
            "labels": [{
                "forecast_id": prediction["forecast_id"],
                "trade_date": "2026-08-20",
                "correct": True,
                "official_label_status": "provisional",
                "official_open_to_close_return": 0.01,
            }],
        }
        prospective = build_prospective_evaluations(published, label_document)
        self.assertTrue(prospective[0]["score_eligible"])
        self.assertIsNone(prospective[0]["p_committee_hit"])
        shadow_copy = {**published, "mode": "shadow"}
        self.assertFalse(build_prospective_evaluations(shadow_copy, label_document)[0]["score_eligible"])
        value["predictions"][0]["confidence"] = 0.9
        with self.assertRaises(Exception):
            freeze_run(
                run_input=value, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
            )
        del value["predictions"][0]["confidence"]
        value["reports_by_symbol"]["AAPL"][0]["as_of_et"] = "2026-08-20T08:55:01-04:00"
        with self.assertRaises(Exception):
            freeze_run(
                run_input=value, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
            )
        value["reports_by_symbol"]["AAPL"][0]["as_of_et"] = self.cutoff
        value["evidence"] = [
            self.record("e1", "event", captured_at="2026-08-20T08:54:00-04:00"),
            self.record("e2", "price_volume", raw_file_path=second_raw.name, raw_sha256=second_sha),
        ]
        value["created_at"] = "2026-08-20T08:56:00-04:00"
        shadow = freeze_run(
            run_input=value, evidence_root=self.root,
            integration_policy=self.integration_policy(),
            candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
        )
        self.assertEqual(shadow["mode"], "shadow")
        self.assertFalse(shadow["predictions"][0]["score_eligible"])
        value["evidence"] = [
            self.record("e1", "event"),
            self.record("e2", "price_volume", raw_file_path=second_raw.name, raw_sha256=second_sha),
        ]
        value["created_at"] = "2026-08-20T08:29:59-04:00"
        with self.assertRaises(Exception):
            freeze_run(
                run_input=value, evidence_root=self.root,
                integration_policy=self.integration_policy(),
                candidate_intake_sha256="c" * 64, isolation_status=self.isolation_status(),
            )


if __name__ == "__main__":
    unittest.main()
