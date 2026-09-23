from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shaq_daily_oracle.data_providers import DataProfile, DataProviderError, YFinanceProvider
from shaq_daily_oracle.research_collection import ResearchCollectionError, collect_research_evidence
from tests.test_research_collection import FakeEvents, FakeMarket, FakeMetadata


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
OBSERVED = datetime(2026, 9, 4, 8, 45, tzinfo=ZoneInfo("America/New_York"))


class TrackingMarket(FakeMarket):
    def __init__(self, *, fail_history_call: int | None = None):
        self.history_calls: list[list[str]] = []
        self.premarket_calls: list[list[str]] = []
        self.fail_history_call = fail_history_call

    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        self.history_calls.append(list(symbols))
        if len(self.history_calls) == self.fail_history_call:
            raise DataProviderError("fixture history failed", diagnostic={"kind": "timeout"})
        rows = super().history(symbols, start=start, end=end, interval=interval, prepost=prepost)
        if "ABBV" in rows:
            rows["ABBV"] = []
        return rows

    def recent_intraday(self, symbols, *, cutoff):
        self.premarket_calls.append(list(symbols))
        rows = super().recent_intraday(symbols, cutoff=cutoff)
        if "ABBV" in rows:
            rows["ABBV"] = []
        return rows

    def option_surface(self, symbol):
        if symbol == "ABNB":
            return {"symbol": symbol, "status": "provider_error", "error_type": "TimeoutError"}
        return {
            "symbol": symbol, "status": "collected",
            "quote_timestamp_status": "unavailable", "quote_freshness_eligible": False,
            "quality": {"source_contract_count": 2, "retained_contract_count": 2,
                        "contracts_with_valid_two_sided_price": 1},
            "expiries": {"2026-09-18": {"status": "collected"}},
        }


class ObservableYahoo(YFinanceProvider):
    """Local Yahoo-shaped provider: no network, real observer interface."""

    def history(self, symbols, *, start, end, interval='1d', prepost=False):
        if self.progress_observer is not None:
            self.progress_observer(stage='data_retry_scheduled', source='yfinance',
                                   request_stage='history', symbol=symbols[0],
                                   attempt=2, max_attempts=2,
                                   next_retry_at='2026-09-04T12:45:00+00:00')
        return FakeMarket.history(self, symbols, start=start, end=end,
                                  interval=interval, prepost=prepost)

    def recent_intraday(self, symbols, *, cutoff):
        return FakeMarket.recent_intraday(self, symbols, cutoff=cutoff)

    def option_surface(self, symbol):
        return FakeMarket.option_surface(self, symbol)


class CollectionProgressTests(unittest.TestCase):
    def _profile(self, root: Path) -> DataProfile:
        universe = root / "universe.csv"
        with universe.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "instrument", "company_name", "gics_sector", "gics_sub_industry",
                "cik_company_id", "known_from_utc",
            ])
            writer.writeheader()
            for symbol, sector in (
                ("AAPL", "Information Technology"),
                ("ABBV", "Health Care"),
                ("ABNB", "Consumer Discretionary"),
            ):
                writer.writerow({"instrument": symbol, "company_name": symbol,
                                 "gics_sector": sector, "gics_sub_industry": "Fixture",
                                 "cik_company_id": "CIK:0000000001",
                                 "known_from_utc": "2026-01-01T00:00:00Z"})
        return DataProfile("progress-fixture", str(universe), batch_size=2,
                           maximum_candidates=3)

    def _collect(self, root: Path, profile: DataProfile, market: TrackingMarket, observer):
        return collect_research_evidence(
            root=root, package_root=PACKAGE_ROOT, profile=profile,
            sec_identity="Research test@example.edu", observed_at=OBSERVED,
            market_provider=market, metadata_provider=FakeMetadata(),
            event_provider=FakeEvents(), observer=observer,
        )

    def test_progress_counts_completed_requests_separately_from_returned_data(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            profile = self._profile(root)
            market = TrackingMarket()
            events = []
            evidence = self._collect(root / "first", profile, market,
                                     lambda **event: events.append(event))

            self.assertTrue(all(len(group) <= profile.batch_size for group in market.history_calls))
            self.assertTrue(all(len(group) <= profile.batch_size for group in market.premarket_calls))
            history = [row for row in events if row.get("component") == "history"]
            premarket = [row for row in events if row.get("component") == "premarket"]
            self.assertEqual(history[-1]["status"], "complete")
            self.assertEqual(history[-1]["completed"], len(market.history_calls))
            self.assertEqual(history[-1]["total"], len(market.history_calls))
            self.assertLess(history[-1]["returned_symbol_count"],
                            sum(len(group) for group in market.history_calls))
            self.assertEqual(premarket[-1]["completed"], len(market.premarket_calls))
            self.assertEqual(premarket[-1]["total"], len(market.premarket_calls))
            self.assertLess(premarket[-1]["returned_symbol_count"],
                            sum(len(group) for group in market.premarket_calls))
            options = [row for row in events if row.get("component") == "options"]
            self.assertEqual(options[-1]["status"], "failed")
            self.assertEqual(options[-1]["completed"], 1)
            self.assertEqual(options[-1]["checked"], 2)
            self.assertEqual(options[-1]["total"], 2)
            self.assertEqual(options[-1]["chain_count"], 1)
            self.assertFalse(any(row["domain"] == "derivatives" for row in evidence.lineage["records"]))

            ignored = self._collect(root / "ignored", profile, TrackingMarket(),
                                    lambda **event: (_ for _ in ()).throw(OSError("observer")))
            self.assertEqual(evidence.manifest["evidence_hash"], ignored.manifest["evidence_hash"])

    def test_failed_history_group_does_not_emit_false_completion(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            profile = self._profile(root)
            events = []
            with self.assertRaises(ResearchCollectionError):
                self._collect(root / "failed", profile,
                              TrackingMarket(fail_history_call=2),
                              lambda **event: events.append(event))
            history = [row for row in events if row.get("component") == "history"]
            self.assertEqual(history[-1]["status"], "failed")
            self.assertEqual(history[-1]["completed"], 1)
            self.assertGreater(history[-1]["total"], 1)
            self.assertFalse(any(row["status"] == "complete" for row in history))

    def test_yahoo_retry_events_reach_progress_without_affecting_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            profile = self._profile(root)
            events = []
            observed = self._collect(root / 'observed', profile, ObservableYahoo(profile),
                                     lambda **event: events.append(event))
            self.assertTrue(any(row.get('stage') == 'data_retry_scheduled'
                                and row.get('source') == 'yfinance' for row in events))
            ignored = self._collect(root / 'ignored', profile, ObservableYahoo(profile),
                                    lambda **event: (_ for _ in ()).throw(OSError('display unavailable')))
            baseline = self._collect(root / 'baseline', profile, ObservableYahoo(profile), None)
            self.assertEqual(observed.manifest['evidence_hash'], ignored.manifest['evidence_hash'])
            self.assertEqual(observed.manifest['evidence_hash'], baseline.manifest['evidence_hash'])


if __name__ == "__main__":
    unittest.main()
