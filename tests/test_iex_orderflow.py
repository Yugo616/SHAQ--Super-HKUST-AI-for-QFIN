from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.iex_orderflow import compute_iex_orderflow  # noqa: E402


def iex_quote(timestamp: str, *, bp: float = 100.0, bs: int = 3,
              ap: float = 100.1, ask_size: int = 4) -> dict:
    # Synthetic fixture in Alpaca's documented quote-message shape; not a live feed.
    return {"T": "q", "S": "AAPL", "bx": "V", "bp": bp, "bs": bs,
            "ax": "V", "ap": ap, "as": ask_size, "c": ["R"], "z": "C", "t": timestamp}


class IexOrderflowTests(unittest.TestCase):
    def test_hand_calculated_top_of_book_ofi_uses_iex_round_lots(self):
        quotes = [
            {"T": "q", "S": "AAPL", "bx": "V", "bp": 100.0, "bs": 3,
             "ax": "V", "ap": 100.1, "as": 4, "t": "2026-10-02T12:40:00.000000001Z"},
            {"T": "q", "S": "AAPL", "bx": "V", "bp": 100.0, "bs": 5,
             "ax": "V", "ap": 100.1, "as": 2, "t": "2026-10-02T12:40:00.000000002Z"},
            {"T": "q", "S": "AAPL", "bx": "V", "bp": 100.1, "bs": 2,
             "ax": "V", "ap": 100.2, "as": 3, "t": "2026-10-02T12:40:00.000000003Z"},
        ]
        result = compute_iex_orderflow(
            quotes, symbol="AAPL", cutoff="2026-10-02T08:50:00-04:00"
        )
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["ofi_round_lots"], 8)
        self.assertEqual(result["event_count"], 2)
        self.assertAlmostEqual(result["average_visible_depth_round_lots_per_side"], 19 / 6)
        self.assertAlmostEqual(result["ofi_over_average_depth"], 48 / 19)
        self.assertAlmostEqual(result["first_mid_price"], 100.05)
        self.assertAlmostEqual(result["last_mid_price"], 100.15)
        self.assertAlmostEqual(result["mid_price_change"], 0.1)
        self.assertAlmostEqual(
            result["average_spread_bps"],
            ((0.1 / 100.05) + (0.1 / 100.05) + (0.1 / 100.15)) * 10_000 / 3,
        )
        self.assertEqual(result["inference_scope"], "observed_iex_quote_window_only")
        self.assertFalse(result["full_session_coverage_verified"])
        self.assertEqual(result["scope"], "IEX single venue")
        self.assertFalse(result["full_market_capital_flow"])
        self.assertFalse(result["native_trade_aggressor"])
        self.assertFalse(result["data_origin_verified"])

    def test_no_quote_or_only_one_quote_is_unavailable_not_zero_flow(self):
        cutoff = "2026-10-02T08:50:00-04:00"
        for quotes in ([], [iex_quote("2026-10-02T12:40:00Z")]):
            with self.subTest(quotes=len(quotes)):
                result = compute_iex_orderflow(quotes, symbol="AAPL", cutoff=cutoff)
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["ofi_round_lots"])
                self.assertEqual(result["event_count"], 0)
                self.assertIsNone(result["average_visible_depth_round_lots_per_side"])
                self.assertIsNone(result["ofi_over_average_depth"])
                self.assertIsNone(result["first_mid_price"])
                self.assertIsNone(result["last_mid_price"])
                self.assertIsNone(result["mid_price_change"])
                self.assertIsNone(result["average_spread_bps"])

    def test_official_sip_quote_example_is_rejected_as_not_iex(self):
        # Alpaca's published SIP example has different bid and ask venues.
        sip_example = {"T": "q", "S": "AMD", "bx": "U", "bp": 87.66, "bs": 1,
                       "ax": "Q", "ap": 87.68, "as": 4,
                       "t": "2021-02-22T15:51:45.335689322Z", "c": ["R"], "z": "C"}
        with self.assertRaises(ValueError):
            compute_iex_orderflow(
                [sip_example], symbol="AMD", cutoff="2021-02-22T11:00:00-05:00"
            )

    def test_duplicate_quote_is_ignored_but_conflicting_same_time_is_rejected(self):
        first = iex_quote("2026-10-02T12:40:00.000000001Z")
        second = iex_quote("2026-10-02T12:40:00.000000002Z", bs=5)
        result = compute_iex_orderflow(
            [first, first.copy(), second], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
        )
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["ofi_round_lots"], 2)
        with self.assertRaises(ValueError):
            compute_iex_orderflow(
                [first, {**first, "bs": 9}], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
            )

    def test_out_of_order_quotes_are_rejected_and_after_cutoff_quotes_excluded(self):
        first = iex_quote("2026-10-02T12:40:00Z")
        older = iex_quote("2026-10-02T12:39:59Z", bs=7)
        with self.assertRaises(ValueError):
            compute_iex_orderflow(
                [first, older], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
            )
        future = iex_quote("2026-10-02T12:50:00.000000001Z", bs=100)
        result = compute_iex_orderflow(
            [first, future], symbol="AAPL", cutoff="2026-10-02T08:50:00-04:00"
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["ofi_round_lots"])
        self.assertIsNone(result["ofi_over_average_depth"])
        self.assertIsNone(result["average_spread_bps"])

    def test_invalid_depth_and_crossed_book_break_continuity(self):
        first = iex_quote("2026-10-02T12:40:00Z")
        invalid = iex_quote("2026-10-02T12:40:01Z", bs=0)
        last = iex_quote("2026-10-02T12:40:02Z", bs=9)
        result = compute_iex_orderflow(
            [first, invalid, last], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["ofi_round_lots"])
        crossed = iex_quote("2026-10-02T12:40:01Z", bp=101.0, ap=100.0)
        result = compute_iex_orderflow(
            [first, crossed, last], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
        )
        self.assertEqual(result["status"], "unavailable")

    def test_missing_venue_on_either_side_is_rejected(self):
        first = iex_quote("2026-10-02T12:40:00Z")
        for field in ("bx", "ax"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                compute_iex_orderflow(
                    [{**first, field: "Q"}], symbol="AAPL", cutoff="2026-10-02T12:50:00Z"
                )


if __name__ == "__main__":
    unittest.main()
