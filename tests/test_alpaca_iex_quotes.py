from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.alpaca_iex_quotes import _default_fetch, collect_iex_quotes  # noqa: E402


def rest_quote(timestamp: str, *, size: int) -> dict:
    # Synthetic historical REST quote (no websocket T/S fields), never live data.
    return {"bx": "V", "bp": 100.0, "bs": size, "ax": "V", "ap": 100.1,
            "as": 4, "c": ["R"], "z": "C", "t": timestamp}


class AlpacaIexQuoteTests(unittest.TestCase):
    def test_no_credentials_stops_before_transport(self):
        calls = []

        def fake_fetch(request, timeout, max_bytes):
            calls.append(request)
            raise AssertionError("transport must not run")

        with self.assertRaises(ValueError):
            collect_iex_quotes(
                key_id="", secret="", symbols=["AAPL"],
                cutoff="2026-10-02T08:50:00-04:00",
                observed_at="2026-10-02T08:49:00-04:00", window_seconds=60,
                max_pages_per_symbol=3, timeout_seconds=2,
                max_response_bytes=4096, max_attempts=2, fetch=fake_fetch,
            )
        self.assertEqual(calls, [])

    def test_paginates_each_symbol_and_preserves_raw_source_hashes(self):
        bodies = [
            json.dumps({"quotes": {"AAPL": [rest_quote("2026-10-02T12:49:00Z", size=3)]},
                        "next_page_token": "page-two"}).encode(),
            json.dumps({"quotes": {"AAPL": [rest_quote("2026-10-02T12:49:01Z", size=5)]},
                        "next_page_token": None}).encode(),
            json.dumps({"quotes": {"MSFT": [rest_quote("2026-10-02T12:49:00Z", size=2)]},
                        "next_page_token": None}).encode(),
        ]
        requests = []

        def fake_fetch(request, timeout, max_bytes):
            requests.append((request, timeout))
            return bodies[len(requests) - 1]

        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL", "MSFT"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
            max_pages_per_symbol=3, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2, fetch=fake_fetch,
            clock=lambda: datetime(2026, 10, 2, 12, 49, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(len(requests), 3)
        for request, timeout in requests:
            self.assertEqual(timeout, 2)
            self.assertEqual(urlparse(request.full_url).path, "/v2/stocks/quotes")
            query = parse_qs(urlparse(request.full_url).query)
            self.assertEqual(query["feed"], ["iex"])
            self.assertEqual(query["sort"], ["asc"])
            self.assertEqual(query["start"], ["2026-10-02T12:48:30Z"])
            self.assertEqual(query["end"], ["2026-10-02T12:49:30Z"])
            self.assertNotIn("test-secret", request.full_url)
            self.assertEqual(request.get_header("Apca-api-key-id"), "test-id")
            self.assertEqual(request.get_header("Apca-api-secret-key"), "test-secret")
        self.assertEqual(parse_qs(urlparse(requests[0][0].full_url).query)["symbols"], ["AAPL"])
        self.assertEqual(parse_qs(urlparse(requests[1][0].full_url).query)["page_token"], ["page-two"])
        self.assertEqual(parse_qs(urlparse(requests[2][0].full_url).query)["symbols"], ["MSFT"])
        self.assertEqual(result["symbols"]["AAPL"]["calculation"]["ofi_round_lots"], 2)
        self.assertEqual(result["symbols"]["MSFT"]["calculation"]["status"], "unavailable")
        self.assertEqual(result["symbols"]["AAPL"]["raw_pages"][0]["sha256"],
                         hashlib.sha256(bodies[0]).hexdigest())
        self.assertEqual(result["symbols"]["AAPL"]["raw_pages"][0]["body_utf8"], bodies[0].decode())
        self.assertEqual(result["symbols"]["AAPL"]["normalization"],
                         "historical_rest_quote_plus_T_q_and_S_symbol")
        self.assertTrue(result["formal_cutoff_eligible"])
        self.assertNotIn("test-secret", repr(result))

    def test_window_filter_and_post_cutoff_request_remain_diagnostic(self):
        body = json.dumps({"quotes": {"AAPL": [
            rest_quote("2026-10-02T12:48:59.999999999Z", size=99),
            rest_quote("2026-10-02T12:49:00Z", size=3),
            rest_quote("2026-10-02T12:49:01Z", size=5),
            rest_quote("2026-10-02T12:50:00.000000001Z", size=99),
        ]}, "next_page_token": None}).encode()
        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T09:00:00-04:00", window_seconds=60,
            max_pages_per_symbol=2, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2,
            fetch=lambda request, timeout, max_bytes: body,
            clock=lambda: datetime(2026, 10, 2, 13, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(result["symbols"]["AAPL"]["quote_count"], 2)
        self.assertEqual(result["symbols"]["AAPL"]["calculation"]["ofi_round_lots"], 2)
        self.assertFalse(result["formal_cutoff_eligible"])
        self.assertFalse(result["symbols"]["AAPL"]["formal_cutoff_eligible"])
        self.assertTrue(result["post_cutoff_replay"])
        self.assertEqual(result["symbols"]["AAPL"]["raw_pages"][0]["body_utf8"], body.decode())

    def test_repeated_page_token_fails_only_affected_symbol(self):
        calls = 0

        def fake_fetch(request, timeout, max_bytes):
            nonlocal calls
            calls += 1
            symbol = parse_qs(urlparse(request.full_url).query)["symbols"][0]
            if symbol == "AAPL":
                return json.dumps({"quotes": {"AAPL": []},
                                   "next_page_token": "repeat"}).encode()
            return json.dumps({"quotes": {"MSFT": [
                rest_quote("2026-10-02T12:49:00Z", size=3),
                rest_quote("2026-10-02T12:49:01Z", size=4),
            ]}, "next_page_token": None}).encode()

        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL", "MSFT"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
            max_pages_per_symbol=3, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2, fetch=fake_fetch,
            clock=lambda: datetime(2026, 10, 2, 12, 49, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(calls, 3)
        self.assertEqual(result["symbols"]["AAPL"]["status"], "provider_error")
        self.assertEqual(result["symbols"]["MSFT"]["status"], "computed")
        self.assertFalse(result["symbols"]["AAPL"]["formal_cutoff_eligible"])
        self.assertTrue(result["symbols"]["MSFT"]["formal_cutoff_eligible"])

    def test_wrong_symbol_payload_cannot_be_normalized_into_requested_symbol(self):
        body = json.dumps({"quotes": {"MSFT": [
            rest_quote("2026-10-02T12:49:00Z", size=3),
            rest_quote("2026-10-02T12:49:01Z", size=4),
        ]}, "next_page_token": None}).encode()
        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
            max_pages_per_symbol=2, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2,
            fetch=lambda request, timeout, max_bytes: body,
            clock=lambda: datetime(2026, 10, 2, 12, 49, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(result["symbols"]["AAPL"]["status"], "provider_error")
        self.assertIsNone(result["symbols"]["AAPL"]["calculation"]["ofi_round_lots"])

    def test_request_boundary_preserves_nanosecond_cutoff(self):
        urls = []

        def fake_fetch(request, timeout, max_bytes):
            urls.append(request.full_url)
            return json.dumps({"quotes": {"AAPL": []}, "next_page_token": None}).encode()

        collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL"],
            cutoff="2026-10-02T12:50:00.000000002Z",
            observed_at="2026-10-02T12:50:00.000000003Z", window_seconds=60,
            max_pages_per_symbol=2, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2, fetch=fake_fetch,
            clock=lambda: datetime(2026, 10, 2, 12, 50, 1, tzinfo=timezone.utc),
        )
        query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(query["start"], ["2026-10-02T12:49:00.000000002Z"])
        self.assertEqual(query["end"], ["2026-10-02T12:50:00.000000002Z"])

    def test_transient_http_error_retries_within_bound(self):
        attempts = 0

        def fake_fetch(request, timeout, max_bytes):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HTTPError(request.full_url, 429, "rate limited", None, None)
            return json.dumps({"quotes": {"AAPL": [
                rest_quote("2026-10-02T12:49:00Z", size=3),
                rest_quote("2026-10-02T12:49:01Z", size=4),
            ]}, "next_page_token": None}).encode()

        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
            max_pages_per_symbol=2, timeout_seconds=2,
            max_response_bytes=4096, max_attempts=2, fetch=fake_fetch,
            clock=lambda: datetime(2026, 10, 2, 12, 49, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(result["symbols"]["AAPL"]["status"], "computed")

    def test_oversize_response_and_invalid_symbol_fail_closed(self):
        calls = []

        def fake_fetch(request, timeout, max_bytes):
            calls.append(request)
            return b"x" * 50

        with self.assertRaises(ValueError):
            collect_iex_quotes(
                key_id="test-id", secret="test-secret", symbols=["AAPL,MSFT"],
                cutoff="2026-10-02T08:50:00-04:00",
                observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
                max_pages_per_symbol=2, timeout_seconds=2,
                max_response_bytes=10, max_attempts=2, fetch=fake_fetch,
            )
        self.assertEqual(calls, [])
        result = collect_iex_quotes(
            key_id="test-id", secret="test-secret", symbols=["AAPL"],
            cutoff="2026-10-02T08:50:00-04:00",
            observed_at="2026-10-02T08:49:30-04:00", window_seconds=60,
            max_pages_per_symbol=2, timeout_seconds=2,
            max_response_bytes=10, max_attempts=2, fetch=fake_fetch,
            clock=lambda: datetime(2026, 10, 2, 12, 49, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(result["symbols"]["AAPL"]["status"], "provider_error")
        self.assertEqual(result["symbols"]["AAPL"]["page_count"], 0)

    def test_default_transport_denies_redirect_and_caps_bytes_read(self):
        observed = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, length):
                observed["read_length"] = length
                return b"{}"

        class Opener:
            def open(self, request, timeout):
                observed["timeout"] = timeout
                return Response()

        def fake_build_opener(handler):
            observed["redirect_result"] = handler.redirect_request(
                Request("https://data.alpaca.markets/v2/stocks/quotes"), None, 302,
                "redirect", {}, "https://elsewhere.example/quotes",
            )
            return Opener()

        with patch("shaq_daily_oracle.alpaca_iex_quotes.build_opener", fake_build_opener):
            body = _default_fetch(Request("https://data.alpaca.markets/v2/stocks/quotes"), 2, 10)
        self.assertEqual(body, b"{}")
        self.assertIsNone(observed["redirect_result"])
        self.assertEqual(observed["read_length"], 11)
        self.assertEqual(observed["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
