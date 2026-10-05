from __future__ import annotations

import hashlib
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from shaq_daily_oracle.occ_options import OccOptionsError, fetch_occ_series_search, parse_occ_series_search


SAMPLE = (
    b"Series Search Results for WDC\r\n"
    b"Products for this underlying symbol are traded on: CBOE\r\n"
    b"\t\tSeries/contract\t\tStrike\t\t\tOpen Interest\r\n"
    b"ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit\t\r\n"
    b"WDC   \t\t2026\t10\t02\t250\t000\tC P \t3\t113\t25000000\r\n"
    b"WDC   \t\t2026\t10\t09\t574\t700\t  P \t0\t53\t25000000\r\n"
    b"WDC1  \t\t2026\t10\t16\t425\t000\tC P \t1250\t1250\t25000000\r\n"
)
CAPTURED = datetime(2026, 10, 2, 8, 44, tzinfo=ZoneInfo("America/New_York"))


class OccOptionsTests(unittest.TestCase):
    def test_parses_standard_series_and_preserves_raw_provenance(self):
        result = parse_occ_series_search(SAMPLE, symbol="WDC", captured_at=CAPTURED)

        self.assertEqual(result["status"], "collected")
        self.assertEqual(result["symbol"], "WDC")
        self.assertEqual(result["source_sha256"], hashlib.sha256(SAMPLE).hexdigest())
        self.assertEqual(result["asof_session"], "2026-10-01")
        self.assertEqual(result["asof_status"], "inferred_previous_session_not_in_payload")
        self.assertEqual(result["excluded_adjusted_root_count"], 1)
        self.assertEqual(result["contracts"], [
            {"expiration": "2026-10-02", "strike": "250.000", "option_type": "call", "open_interest": 3},
            {"expiration": "2026-10-02", "strike": "250.000", "option_type": "put", "open_interest": 113},
            {"expiration": "2026-10-09", "strike": "574.700", "option_type": "put", "open_interest": 53},
        ])
        self.assertEqual(result["by_expiry_summary"], [
            {"expiration": "2026-10-02", "call_contract_count": 1, "put_contract_count": 1,
             "call_open_interest": 3, "put_open_interest": 113},
            {"expiration": "2026-10-09", "call_contract_count": 0, "put_contract_count": 1,
             "call_open_interest": 0, "put_open_interest": 53},
        ])
        self.assertNotIn("implied_volatility", result)
        self.assertNotIn("bid", result)

    def test_rejects_wrong_symbol_or_truncated_report(self):
        with self.assertRaises(OccOptionsError):
            parse_occ_series_search(SAMPLE.replace(b"Results for WDC", b"Results for APP"),
                                    symbol="WDC", captured_at=CAPTURED)
        with self.assertRaises(OccOptionsError):
            parse_occ_series_search(SAMPLE.split(b"WDC   ")[0], symbol="WDC", captured_at=CAPTURED)

    def test_rejects_invalid_strike_and_shifted_columns(self):
        for corrupted in (
            SAMPLE.replace(b"574\t700", b"574\t1000"),
            SAMPLE.replace(b"250\t000", b"0\t000"),
            SAMPLE.replace(b"WDC   \t\t2026", b"WDC   \t2026"),
        ):
            with self.subTest(corrupted=corrupted[-95:]), self.assertRaises(OccOptionsError):
                parse_occ_series_search(corrupted, symbol="WDC", captured_at=CAPTURED)

    def test_duplicate_series_same_interest_is_idempotent_but_conflict_is_rejected(self):
        row = b"WDC   \t\t2026\t10\t02\t250\t000\tC P \t3\t113\t25000000\r\n"
        repeated = SAMPLE + row
        result = parse_occ_series_search(repeated, symbol="WDC", captured_at=CAPTURED)
        self.assertEqual(len(result["contracts"]), 3)
        with self.assertRaises(OccOptionsError):
            parse_occ_series_search(SAMPLE + row.replace(b"\t113\t", b"\t114\t"),
                                    symbol="WDC", captured_at=CAPTURED)

    def test_fetch_uses_documented_symbol_query_and_caps_response(self):
        seen = []

        class Response:
            status_code = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def iter_bytes(self):
                yield SAMPLE

        def streamer(method, url, *, timeout):
            seen.append((method, url, timeout))
            return Response()

        payload = fetch_occ_series_search("WDC", timeout_seconds=7, max_bytes=1000, streamer=streamer)
        self.assertEqual(payload, SAMPLE)
        self.assertEqual(seen, [("GET", "https://marketdata.theocc.com/series-search?symbolType=U&symbol=WDC", 7)])
        with self.assertRaises(OccOptionsError):
            fetch_occ_series_search("WDC", max_bytes=10, streamer=streamer)
        with self.assertRaises(OccOptionsError):
            fetch_occ_series_search("WDC&symbol=APP", streamer=streamer)

    def test_fetch_retries_once_for_server_error_but_not_for_forbidden(self):
        calls = []

        class Response:
            headers = {}

            def __init__(self, status):
                self.status_code = status

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def iter_bytes(self):
                yield SAMPLE

        def flaky(method, url, *, timeout):
            calls.append(method)
            return Response(503 if len(calls) == 1 else 200)

        self.assertEqual(fetch_occ_series_search("WDC", streamer=flaky), SAMPLE)
        self.assertEqual(len(calls), 2)
        calls.clear()

        def forbidden(method, url, *, timeout):
            calls.append(method)
            return Response(403)

        with self.assertRaises(OccOptionsError):
            fetch_occ_series_search("WDC", streamer=forbidden)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
