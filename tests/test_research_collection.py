from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shaq_daily_oracle.data_providers import DataProfile
from shaq_daily_oracle.research_batch import _tasks_for_domain
from shaq_daily_oracle.research_collection import (
    ResearchCollectionError,
    _premarket_state,
    collect_research_evidence,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class FakeMarket:
    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        return {
            symbol: [
                {"timestamp": "2026-09-02T16:00:00-04:00", "open": 99, "close": 100, "volume": 1000},
                {"timestamp": "2026-09-03T16:00:00-04:00", "open": 100, "close": 101, "volume": 1100},
            ]
            for symbol in symbols
        }

    def recent_intraday(self, symbols, *, cutoff):
        return {
            symbol: [{
                "timestamp": "2026-09-04T08:40:00-04:00",
                "open": 101, "close": 111 if symbol == "AAPL" else 101,
                "volume": 5000 if symbol == "AAPL" else 100,
            }]
            for symbol in symbols
        }

    def option_surface(self, symbol):
        return {
            "symbol": symbol, "status": "no_data", "expiries": {},
            "quality": {
                "source_contract_count": 0,
                "retained_contract_count": 0,
                "contracts_with_valid_two_sided_price": 0,
            },
        }


class FakeMetadata:
    def metadata(self, symbols):
        return {symbol: {"exchange": "NASDAQ"} for symbol in symbols}


class FakeEvents:
    def recent_events(self, members, *, cutoff, since):
        return {member.symbol: [] for member in members}


class ResearchCollectionTests(unittest.TestCase):
    def test_collection_uses_configured_history_window(self):
        import shutil
        from datetime import date
        starts = []
        class RecordingMarket(FakeMarket):
            def history(self, symbols, **kwargs):
                starts.append(kwargs['start'])
                return super().history(symbols, **kwargs)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package = root/'package'
            shutil.copytree(PACKAGE_ROOT/'config', package/'config')
            path = package/'config/price-history.json'
            value = json.loads(path.read_text())
            value['lookback_calendar_days'] = 365
            path.write_text(json.dumps(value))
            collect_research_evidence(root=root/'evidence', package_root=package,
                profile=self.profile(), sec_identity='Research test@example.edu',
                observed_at=datetime(2026,9,4,8,45,tzinfo=ZoneInfo('America/New_York')),
                market_provider=RecordingMarket(), metadata_provider=FakeMetadata(), event_provider=FakeEvents())
            self.assertTrue(starts)
            self.assertEqual(set(starts), {date(2025,9,4)})

    def test_premarket_state_distinguishes_provider_zero_from_missing_volume(self):
        cutoff = datetime(2026, 9, 17, 8, 50, tzinfo=ZoneInfo("America/New_York"))
        common = {
            "timestamp": "2026-09-17T08:40:00-04:00",
            "open": 250.0,
            "close": 251.0,
        }

        reported_zero = _premarket_state(
            [{**common, "volume": 0}], session_date=cutoff.date(), cutoff=cutoff,
            previous_close=240.0,
        )
        missing = _premarket_state(
            [common], session_date=cutoff.date(), cutoff=cutoff,
            previous_close=240.0,
        )

        self.assertEqual(reported_zero["volume_status"], "volume_unavailable")
        self.assertIsNone(reported_zero["observed_volume"])
        self.assertEqual(reported_zero["volume_observation_count"], 1)
        self.assertEqual(reported_zero["zero_volume_bar_count"], 1)
        self.assertFalse(reported_zero["volume_ranking_eligible"])
        self.assertEqual(missing["volume_status"], "missing")
        self.assertIsNone(missing["observed_volume"])
        self.assertEqual(missing["missing_volume_bar_count"], 1)
        self.assertFalse(missing["volume_ranking_eligible"])

    def test_premarket_state_excludes_partially_missing_volume_from_ranking(self):
        cutoff = datetime(2026, 9, 17, 8, 50, tzinfo=ZoneInfo("America/New_York"))
        rows = [
            {"timestamp": "2026-09-17T08:35:00-04:00", "close": 250, "volume": 120},
            {"timestamp": "2026-09-17T08:40:00-04:00", "close": 251},
            {"timestamp": "2026-09-17T08:45:00-04:00", "close": 252, "volume": 0},
        ]

        state = _premarket_state(
            rows, session_date=cutoff.date(), cutoff=cutoff, previous_close=240.0,
        )

        self.assertEqual(state["volume_status"], "partially_missing")
        self.assertEqual(state["observed_volume"], 120.0)
        self.assertEqual(state["positive_volume_bar_count"], 1)
        self.assertEqual(state["zero_volume_bar_count"], 1)
        self.assertEqual(state["missing_volume_bar_count"], 1)
        self.assertFalse(state["volume_ranking_eligible"])

    def test_different_screeners_collect_same_union_regardless_of_version_order(self):
        from shaq_daily_oracle.hashing import sha256_payload
        scripts = ['function compute(x){return {symbols:[x.candidates[0].symbol]}}',
                   'function compute(x){return {symbols:[x.candidates[x.candidates.length-1].symbol]}}']
        with tempfile.TemporaryDirectory() as name:
            results=[]
            for i, order in enumerate([scripts, list(reversed(scripts))]):
                evidence=collect_research_evidence(root=Path(name)/str(i), package_root=PACKAGE_ROOT,
                    profile=self.profile(), sec_identity='Research test@example.edu',
                    observed_at=datetime(2026,9,4,8,45,tzinfo=ZoneInfo('America/New_York')),
                    market_provider=FakeMarket(),metadata_provider=FakeMetadata(),event_provider=FakeEvents(),
                    screening_rules={sha256_payload(script):script for script in order})
                results.append(evidence)
            self.assertEqual(len(results[0].candidate_intake['candidates']),2)
            self.assertEqual(results[0].manifest['evidence_hash'],results[1].manifest['evidence_hash'])

    def test_option_surface_without_quote_timestamp_is_archived_but_not_task_eligible(self):
        class MarketWithUntimedOptions(FakeMarket):
            def option_surface(self, symbol):
                return {
                    "symbol": symbol,
                    "status": "collected",
                    "captured_at": "2026-09-04T08:45:00-04:00",
                    "capture_timestamp_semantics": (
                        "provider_response_capture_not_exchange_quote_time"
                    ),
                    "quote_timestamp_status": "unavailable",
                    "quote_freshness_eligible": False,
                    "last_trade_timestamp_semantics": "contract_last_trade_not_quote_time",
                    "directional_flow_semantics": False,
                    "quality": {
                        "source_contract_count": 3,
                        "retained_contract_count": 3,
                        "contracts_with_valid_two_sided_price": 2,
                    },
                    "expiries": {"2026-09-18": {"status": "collected"}},
                }

        with tempfile.TemporaryDirectory() as name:
            evidence = collect_research_evidence(
                root=Path(name) / "evidence",
                package_root=PACKAGE_ROOT,
                profile=self.profile(),
                sec_identity="Research test@example.edu",
                observed_at=datetime(
                    2026, 9, 4, 8, 45, tzinfo=ZoneInfo("America/New_York")
                ),
                market_provider=MarketWithUntimedOptions(),
                metadata_provider=FakeMetadata(),
                event_provider=FakeEvents(),
            )

            tasks = _tasks_for_domain(evidence, "derivatives")
            self.assertTrue(all(task["collection_status"] == "no_data" for task in tasks))
            self.assertFalse(any(
                record["domain"] == "derivatives" for record in evidence.lineage["records"]
            ))
            archived = json.loads(
                (evidence.root / "raw/options/AAPL.json").read_text(encoding="utf-8")
            )
            self.assertEqual(archived["quote_timestamp_status"], "unavailable")
            self.assertEqual(archived["source_contract_count"], 3)
            self.assertEqual(archived["retained_contract_count"], 3)
            self.assertEqual(archived["valid_two_sided_price_count"], 2)
            derivatives_status = next(
                row for row in evidence.manifest["provider_manifest"]["collection_statuses"]
                if row["symbol"] == "AAPL" and row["domain"] == "derivatives"
            )
            self.assertEqual(derivatives_status["status"], "no_data")
            self.assertEqual(derivatives_status["reason"], "missing_exchange_quote_timestamp")
            self.assertEqual(derivatives_status["retained_contract_count"], 3)
            self.assertEqual(derivatives_status["valid_two_sided_price_count"], 2)
            self.assertEqual(derivatives_status["quote_timestamp_status"], "unavailable")

    def profile(self, maximum_candidates=3):
        return DataProfile(
            profile_id="test-free",
            universe_file="config/research-universe.csv",
            maximum_candidates=maximum_candidates,
        )

    def test_free_collection_forms_shared_candidate_set_and_honest_missing_domains(self):
        with tempfile.TemporaryDirectory() as name:
            evidence = collect_research_evidence(
                root=Path(name) / "evidence", package_root=PACKAGE_ROOT,
                profile=self.profile(), sec_identity="Research test@example.edu",
                observed_at=datetime(
                    2026, 9, 4, 8, 45, tzinfo=ZoneInfo("America/New_York")
                ),
                market_provider=FakeMarket(), metadata_provider=FakeMetadata(),
                event_provider=FakeEvents(),
            )
            self.assertEqual(evidence.manifest["cutoff_status"], "on_time")
            self.assertEqual(len(evidence.candidate_intake["candidates"]), 3)
            selected = {row["symbol"]: row for row in evidence.candidate_intake["candidates"]}
            self.assertIn("AAPL", selected)
            self.assertEqual(
                selected["AAPL"]["selection_method"],
                "premarket_stock_minus_sector_absolute_residual",
            )
            capital = _tasks_for_domain(evidence, "capital")
            event = _tasks_for_domain(evidence, "event")
            derivatives = _tasks_for_domain(evidence, "derivatives")
            self.assertTrue(all(row["collection_status"] == "no_data" for row in capital))
            self.assertTrue(all(row["collection_status"] == "not_applicable" for row in event))
            self.assertTrue(all(row["collection_status"] == "no_data" for row in derivatives))
            derivatives_status = next(
                row for row in evidence.manifest["provider_manifest"]["collection_statuses"]
                if row["symbol"] == "AAPL" and row["domain"] == "derivatives"
            )
            self.assertEqual(derivatives_status["reason"], "no_option_chain")
            self.assertEqual(derivatives_status["retained_contract_count"], 0)
            self.assertFalse(evidence.manifest["provider_manifest"]["production_grade_claimed"])
            provider_status = json.loads(
                (evidence.root / "raw/provider-status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                provider_status["capital"]["reason"],
                "requires_authorized_aggressor_and_depth_feed",
            )
            self.assertEqual(
                provider_status["option_trade_flow"]["reason"],
                "surface_without_aggressor_or_open_close_semantics",
            )
            observations = evidence.manifest["provider_manifest"]["premarket_observations"]
            self.assertEqual(observations["AAPL"]["volume_status"], "observed_positive")
            candidate = next(
                row for row in evidence.candidate_intake["candidates"]
                if row["symbol"] == "AAPL"
            )
            self.assertEqual(candidate["premarket_volume"], 5000.0)
            self.assertEqual(candidate["premarket_volume_status"], "observed_positive")

    def test_option_provider_error_is_not_reported_as_empty_chain(self):
        class MarketWithOptionError(FakeMarket):
            def option_surface(self, symbol):
                raise ValueError("unavailable")

        with tempfile.TemporaryDirectory() as name:
            evidence = collect_research_evidence(
                root=Path(name) / "evidence", package_root=PACKAGE_ROOT,
                profile=self.profile(), sec_identity="Research test@example.edu",
                observed_at=datetime(2026, 9, 4, 8, 45, tzinfo=ZoneInfo("America/New_York")),
                market_provider=MarketWithOptionError(), metadata_provider=FakeMetadata(),
                event_provider=FakeEvents(),
            )
            status = next(
                row for row in evidence.manifest["provider_manifest"]["collection_statuses"]
                if row["symbol"] == "AAPL" and row["domain"] == "derivatives"
            )
            self.assertEqual(status["status"], "provider_error")
            self.assertEqual(status["reason"], "provider_error")
            self.assertEqual(status["error_type"], "ValueError")
            self.assertIsNone(status["retained_contract_count"])

    def test_option_quote_time_present_but_not_fresh_has_distinct_reason(self):
        class MarketWithStaleQuote(FakeMarket):
            def option_surface(self, symbol):
                return {
                    "symbol": symbol, "status": "collected",
                    "quote_timestamp_status": "available",
                    "quote_freshness_eligible": False,
                    "quality": {
                        "source_contract_count": 1,
                        "retained_contract_count": 1,
                        "contracts_with_valid_two_sided_price": 1,
                    },
                    "expiries": {"2026-09-18": {"status": "collected"}},
                }

        with tempfile.TemporaryDirectory() as name:
            evidence = collect_research_evidence(
                root=Path(name) / "evidence", package_root=PACKAGE_ROOT,
                profile=self.profile(), sec_identity="Research test@example.edu",
                observed_at=datetime(2026, 9, 4, 8, 45, tzinfo=ZoneInfo("America/New_York")),
                market_provider=MarketWithStaleQuote(), metadata_provider=FakeMetadata(),
                event_provider=FakeEvents(),
            )
            status = next(
                row for row in evidence.manifest["provider_manifest"]["collection_statuses"]
                if row["symbol"] == "AAPL" and row["domain"] == "derivatives"
            )
            self.assertEqual(status["status"], "no_data")
            self.assertEqual(status["reason"], "quote_not_fresh")
            self.assertFalse(any(
                record["domain"] == "derivatives" for record in evidence.lineage["records"]
            ))

    def test_weekend_collection_creates_no_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "evidence"
            with self.assertRaises(ResearchCollectionError):
                collect_research_evidence(
                    root=root, package_root=PACKAGE_ROOT, profile=self.profile(),
                    sec_identity="Research test@example.edu",
                    observed_at=datetime(
                        2026, 9, 5, 8, 45, tzinfo=ZoneInfo("America/New_York")
                    ),
                    market_provider=FakeMarket(), metadata_provider=FakeMetadata(),
                    event_provider=FakeEvents(),
                )
            self.assertFalse(root.exists())

    def test_explicit_manual_weekend_run_is_replay_not_premarket_score(self):
        with tempfile.TemporaryDirectory() as name:
            evidence=collect_research_evidence(root=Path(name)/'evidence',package_root=PACKAGE_ROOT,
                profile=self.profile(),sec_identity='Research test@example.edu',allow_replay=True,
                observed_at=datetime(2026,9,5,8,45,tzinfo=ZoneInfo('America/New_York')),
                market_provider=FakeMarket(),metadata_provider=FakeMetadata(),event_provider=FakeEvents())
            self.assertEqual(evidence.manifest['cutoff_status'],'late_research_only')
            self.assertEqual(evidence.manifest['scheduled_cutoff_et'][:10],'2026-09-04')


if __name__ == "__main__":
    unittest.main()
