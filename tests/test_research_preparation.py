"""Daily history preparation is separate from current premarket collection."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from shaq_daily_oracle.data_providers import DataProviderError
from shaq_daily_oracle.public_data import DailyBarCache
from shaq_daily_oracle.research_preparation import warm_previous_daily_bars


class RecordingProvider:
    def __init__(self):
        self.calls = []

    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        self.calls.append((list(symbols), start, end, interval, prepost))
        return {symbol: [{"timestamp": "2026-09-21T00:00:00",
                          "close": 100, "volume": 10}] for symbol in symbols}


class ResearchPreparationTests(unittest.TestCase):
    def test_warms_only_prior_daily_bars_and_reports_each_completed_symbol(self):
        # Catches accidental premarket/current-day fetches or invented progress.
        source = RecordingProvider()
        events = []
        with tempfile.TemporaryDirectory() as name:
            cache = DailyBarCache(source, Path(name), overlap_days=3)
            result = warm_previous_daily_bars(
                cache, ["AAA", "BBB", "CCC"], start=date(2026, 9, 1),
                target_session_date=date(2026, 9, 22), chunk_size=2,
                deadline_monotonic=10, monotonic=lambda: 1,
                cancelled=lambda: False, progress=events.append,
            )
            self.assertEqual(len(list(Path(name).glob("*.json"))), 3)

        self.assertEqual([call[0] for call in source.calls], [["AAA", "BBB"], ["CCC"]])
        self.assertTrue(all(call[1:] == (date(2026, 9, 1), date(2026, 9, 22), "1d", False)
                            for call in source.calls))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual([event["symbol"] for event in events], ["AAA", "BBB", "CCC"])
        self.assertTrue(all(event["status"] == "collected" for event in events))

    def test_deadline_is_checked_before_each_batch(self):
        # Catches continuing to call the provider after the monotonic deadline.
        source = RecordingProvider()
        samples = iter([0, 0, 10])
        result = warm_previous_daily_bars(
            source, ["AAA", "BBB"], start=date(2026, 9, 1),
            target_session_date=date(2026, 9, 22), chunk_size=1,
            deadline_monotonic=5, monotonic=lambda: next(samples),
            cancelled=lambda: False,
        )
        self.assertEqual(result["status"], "deadline_exceeded")
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(len(source.calls), 1)

    def test_cancel_is_checked_before_each_batch(self):
        # Catches treating cancellation as a completed warmup.
        source = RecordingProvider()
        checks = iter([False, False, True])
        result = warm_previous_daily_bars(
            source, ["AAA", "BBB"], start=date(2026, 9, 1),
            target_session_date=date(2026, 9, 22), chunk_size=1,
            deadline_monotonic=10, monotonic=lambda: 1,
            cancelled=lambda: next(checks),
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(len(source.calls), 1)

    def test_provider_error_reports_failed_batch_without_symbol_blame(self):
        # Catches calling a partial warmup successful or inventing per-symbol errors.
        class FailingProvider(RecordingProvider):
            def history(self, symbols, **kwargs):
                if symbols == ["CCC"]:
                    raise DataProviderError("limited", diagnostic={"kind": "rate_limited"})
                return super().history(symbols, **kwargs)

        source = FailingProvider()
        events = []
        result = warm_previous_daily_bars(
            source, ["AAA", "BBB", "CCC"], start=date(2026, 9, 1),
            target_session_date=date(2026, 9, 22), chunk_size=2,
            deadline_monotonic=10, monotonic=lambda: 1,
            cancelled=lambda: False, progress=events.append,
        )
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(result["pending_symbols"], ["CCC"])
        self.assertEqual(events[-1], {"status": "batch_failed", "symbols": ["CCC"],
                                       "kind": "rate_limited"})

    def test_empty_provider_rows_are_no_data_not_collected(self):
        # Catches manufacturing a completed market observation from an empty row list.
        class PartlyEmptyProvider(RecordingProvider):
            def history(self, symbols, **kwargs):
                rows = super().history(symbols, **kwargs)
                rows["BBB"] = []
                return rows

        events = []
        result = warm_previous_daily_bars(
            PartlyEmptyProvider(), ["AAA", "BBB"], start=date(2026, 9, 1),
            target_session_date=date(2026, 9, 22), chunk_size=2,
            deadline_monotonic=10, monotonic=lambda: 1,
            cancelled=lambda: False, progress=events.append,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["collected_count"], 1)
        self.assertEqual(result["no_data_count"], 1)
        self.assertEqual([event["status"] for event in events], ["collected", "no_data"])


if __name__ == "__main__":
    unittest.main()
