"""Premarket price bars must not make unavailable volume look like zero trading."""

import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shaq_daily_oracle.data_providers import UniverseMember
from shaq_daily_oracle.research_collection import _candidate_rows, _premarket_state
from shaq_daily_oracle.replay import _plain_chinese


ET = ZoneInfo("America/New_York")
SESSION = date(2026, 9, 22)
CUTOFF = datetime(2026, 9, 22, 8, 50, tzinfo=ET)


class PremarketVolumeQualityTests(unittest.TestCase):
    def test_all_zero_provider_bars_preserve_raw_but_expose_unavailable_volume(self):
        # Catches converting provider-reported zeros into a usable zero-volume signal.
        bars = [
            {"timestamp": "2026-09-22T04:00:00-04:00", "close": 100, "volume": 0},
            {"timestamp": "2026-09-22T08:45:00-04:00", "close": 101, "volume": 0},
        ]

        state = _premarket_state(bars, session_date=SESSION, cutoff=CUTOFF,
                                 previous_close=99)

        self.assertEqual(state["status"], "collected")
        self.assertEqual(state["volume_status"], "volume_unavailable")
        self.assertIsNone(state["observed_volume"])
        self.assertEqual(state["zero_volume_bar_count"], 2)
        self.assertEqual([row["volume"] for row in state["bars"]], [0, 0])
        self.assertFalse(state["volume_ranking_eligible"])

    def test_candidate_exposes_null_volume_without_dropping_price_candidate(self):
        # Catches serializing an unqualified provider zero as candidate volume.
        member = UniverseMember("ABC", "ABC Inc", "Technology", "Software", "", "")
        daily = [{"timestamp": "2026-09-21T16:00:00-04:00", "close": 100}]
        stock_intraday = [{"timestamp": "2026-09-22T08:45:00-04:00",
                           "close": 105, "volume": 0}]
        benchmark_intraday = [{"timestamp": "2026-09-22T08:45:00-04:00",
                               "close": 101, "volume": 0}]

        candidates, stocks, _ = _candidate_rows(
            members=[member], stock_daily={"ABC": daily},
            stock_intraday={"ABC": stock_intraday},
            benchmark_daily={"XLK": daily},
            benchmark_intraday={"XLK": benchmark_intraday},
            sector_etf={"Technology": "XLK"}, session_date=SESSION, cutoff=CUTOFF,
            maximum_candidates=1,
        )

        self.assertEqual([row["symbol"] for row in candidates], ["ABC"])
        self.assertIsNone(candidates[0]["premarket_volume"])
        self.assertEqual(candidates[0]["premarket_volume_status"], "volume_unavailable")
        self.assertEqual(stocks["ABC"]["premarket"]["bars"][0]["volume"], 0)

    def test_positive_provider_volume_remains_observable(self):
        # Catches over-broadly discarding real positive-volume observations.
        bars = [{"timestamp": "2026-09-22T08:45:00-04:00",
                 "close": 101, "volume": 75}]

        state = _premarket_state(bars, session_date=SESSION, cutoff=CUTOFF,
                                 previous_close=100)

        self.assertEqual(state["volume_status"], "observed_positive")
        self.assertEqual(state["observed_volume"], 75)
        self.assertTrue(state["volume_ranking_eligible"])

    def test_replay_calls_unavailable_volume_unconfirmed_not_zero_trading(self):
        # Catches dropping the warning when new evidence uses the new status.
        plain = _plain_chinese(
            runtime=Path("."), report={"domain": "price_volume", "verdict": "neutral"},
            candidate={"symbol": "ABC"},
            metrics={"sector_benchmark": "XLK", "residual": 0.05,
                     "volume": None, "volume_status": "volume_unavailable"},
            evidence_records={},
        )
        self.assertIn("不能据此确认没有成交", plain["support"])
        self.assertNotIn("盘前成交0股", plain["support"])


if __name__ == "__main__":
    unittest.main()
