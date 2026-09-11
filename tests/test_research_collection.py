from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from shaq_daily_oracle.data_providers import DataProfile, YFinanceProvider
from shaq_daily_oracle.research_batch import _tasks_for_domain
from shaq_daily_oracle.research_collection import (
    ResearchCollectionError,
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
        return {"symbol": symbol, "status": "no_data", "expiries": {}}


class FakeMetadata:
    def metadata(self, symbols):
        return {symbol: {"exchange": "NASDAQ"} for symbol in symbols}


class FakeEvents:
    def recent_events(self, members, *, cutoff, since):
        return {member.symbol: [] for member in members}


class ResearchCollectionTests(unittest.TestCase):
    def test_fresh_history_clears_only_yfinance_history_response_cache(self):
        cache_clear = Mock()
        fake_yf = SimpleNamespace(data=SimpleNamespace(
            YfData=SimpleNamespace(cache_get=SimpleNamespace(cache_clear=cache_clear))))
        provider = YFinanceProvider(self.profile())
        with patch.object(provider, '_module', return_value=fake_yf), \
                patch.object(provider, 'history', return_value={'AAA': []}) as history:
            self.assertEqual(provider.fresh_history(['AAA'], start=date(2026, 9, 9),
                end=date(2026, 9, 10)), {'AAA': []})
        cache_clear.assert_called_once_with()
        history.assert_called_once()

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
            self.assertFalse(evidence.manifest["provider_manifest"]["production_grade_claimed"])

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
