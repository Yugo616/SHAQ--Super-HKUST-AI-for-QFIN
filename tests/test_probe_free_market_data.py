import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.probe_free_market_data import prepare_run_directory, summarize_yahoo_chart


ET = ZoneInfo("America/New_York")


class FreeMarketDataProbeTests(unittest.TestCase):
    def test_yahoo_summary_reports_exact_premarket_window_and_incomplete_volume(self):
        timestamps = [
            int(datetime(2026, 9, 17, 3, 55, tzinfo=ET).timestamp()),
            int(datetime(2026, 9, 17, 4, 0, tzinfo=ET).timestamp()),
            int(datetime(2026, 9, 17, 8, 45, tzinfo=ET).timestamp()),
            int(datetime(2026, 9, 17, 8, 50, tzinfo=ET).timestamp()),
        ]
        payload = {
            "chart": {"result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{"close": [99, 100, 101, 102],
                                              "volume": [7, 12, None, 9]}]},
            }], "error": None},
        }

        summary = summarize_yahoo_chart(payload, date(2026, 9, 17))

        self.assertEqual(summary["sampling_window_et"],
                         "2026-09-17T04:00:00-04:00/2026-09-17T08:50:00-04:00")
        self.assertEqual(summary["eligible_bar_count"], 3)
        self.assertEqual(summary["volume_observation_count"], 2)
        self.assertEqual(summary["missing_volume_bar_count"], 1)
        self.assertEqual(summary["volume_status"], "partially_missing")
        self.assertFalse(summary["volume_ranking_eligible"])

    def test_output_inside_repository_is_rejected(self):
        repository_root = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            prepare_run_directory(
                repository_root / "probe-output",
                symbol="VRT",
                session_date=date(2026, 9, 17),
                captured_at=datetime(2026, 9, 18, 8, 0, tzinfo=ET),
                repository_root=repository_root,
            )


if __name__ == "__main__":
    unittest.main()
