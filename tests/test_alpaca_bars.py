"""A delayed SIP premarket sample cannot be made fresher by its capture time."""

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from shaq_daily_oracle.alpaca_bars import (
    AlpacaBarsError,
    collect_premarket_volume,
)


ET = ZoneInfo("America/New_York")
SESSION = date(2026, 10, 2)
CUTOFF = datetime(2026, 10, 2, 8, 50, tzinfo=ET)


def bar(stamp, volume):
    return {"t": stamp, "o": 100, "h": 101, "l": 99, "c": 100.5,
            "v": volume, "n": 2, "vw": 100.25}


class AlpacaBarsTests(unittest.TestCase):
    def request(self, client, **overrides):
        arguments = dict(
            symbols=["NVDA", "APP"], session_date=SESSION, cutoff=CUTOFF,
            observed_at=CUTOFF, key_id="key", secret_key="secret",
            base_url="https://example.test", feed="sip", delay_seconds=900,
            safety_margin_seconds=60, delay_source="Alpaca Basic historical SIP",
            client=client, max_pages=3, max_retries=1, retry_backoff_seconds=0,
            clock=lambda: CUTOFF,
        )
        arguments.update(overrides)
        return collect_premarket_volume(**arguments)

    def test_paginates_and_excludes_bars_not_available_at_capture(self):
        # Removing the completed-minute/lag gate would admit the 08:34 bar.
        seen = []
        raw = []

        def handler(request):
            seen.append(request)
            if request.url.params.get("page_token") is None:
                response = httpx.Response(200, json={"bars": {"APP": [
                    bar("2026-10-02T12:01:00Z", 25),
                    bar("2026-10-02T12:34:00Z", 50),
                ]}, "next_page_token": "next"})
            else:
                response = httpx.Response(200, json={"bars": {"NVDA": [
                    bar("2026-10-02T12:33:00Z", 75),
                ]}, "next_page_token": None})
            raw.append(response.content)
            return response

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = self.request(client)

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].url.params["symbols"], "APP,NVDA")
        self.assertEqual(seen[0].url.params["timeframe"], "1Min")
        self.assertEqual(seen[0].url.params["adjustment"], "raw")
        self.assertEqual(seen[0].url.params["feed"], "sip")
        self.assertEqual(seen[0].url.params["end"], "2026-10-02T08:34:00-04:00")
        self.assertEqual(seen[1].url.params["page_token"], "next")
        self.assertEqual(result["symbols"]["APP"]["observed_volume"], 25)
        self.assertEqual(result["symbols"]["NVDA"]["observed_volume"], 75)
        self.assertEqual(result["symbols"]["APP"]["bar_count"], 1)
        self.assertEqual(result["metadata"]["last_complete_minute_end_et"],
                         "2026-10-02T08:34:00-04:00")
        self.assertEqual(result["metadata"]["positive_volume_symbol_count"], 2)
        self.assertEqual(result["metadata"]["provider_returned_symbol_count"], 2)
        self.assertEqual(len(result["raw_pages"]), 2)
        self.assertEqual(result["raw_pages"], raw)
        self.assertEqual(result["metadata"]["delay_source"],
                         "Alpaca Basic historical SIP")

    def test_no_bars_is_unavailable_not_zero_trading(self):
        # A missing symbol cannot become a zero-volume stock in ranking.
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "bars": {"APP": [bar("2026-10-02T12:01:00Z", 10)]},
                "next_page_token": None}))) as client:
            result = self.request(client)

        self.assertEqual(result["symbols"]["NVDA"]["volume_status"], "no_bars")
        self.assertIsNone(result["symbols"]["NVDA"]["observed_volume"])
        self.assertEqual(result["metadata"]["positive_volume_symbol_count"], 1)

    def test_non_boundary_capture_uses_last_whole_minute_end(self):
        # A fractional minute must not be presented as a completed bar boundary.
        observed = datetime(2026, 10, 2, 8, 49, 45, tzinfo=ET)
        requested_ends = []

        def handler(request):
            requested_ends.append(request.url.params["end"])
            return httpx.Response(200, json={"bars": {"APP": [
                bar("2026-10-02T12:32:00Z", 8),
                bar("2026-10-02T12:33:00Z", 9),
            ]}, "next_page_token": None})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = self.request(client, observed_at=observed, clock=lambda: observed)

        self.assertEqual(requested_ends, ["2026-10-02T08:33:00-04:00"])
        self.assertEqual(result["symbols"]["APP"]["observed_volume"], 8)

    def test_rejects_unzoned_or_negative_volume_bar(self):
        # A malformed provider observation must not yield partial output.
        for row in (bar("2026-10-02T08:01:00", 12),
                    bar("2026-10-02T12:01:00Z", -1)):
            with self.subTest(row=row):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={
                        "bars": {"APP": [row]}, "next_page_token": None}))) as client:
                    with self.assertRaises(AlpacaBarsError) as caught:
                        self.request(client)
                self.assertEqual(caught.exception.kind, "protocol_error")

    def test_forbidden_does_not_retry_and_rate_limit_is_bounded(self):
        # Retrying 403 can neither grant rights nor create a valid sample.
        for status, expected_calls in ((403, 1), (429, 2)):
            count = 0

            def handler(request):
                nonlocal count
                count += 1
                return httpx.Response(status)

            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(AlpacaBarsError) as caught:
                        self.request(client)
                self.assertEqual(count, expected_calls)
                self.assertEqual(caught.exception.kind,
                                 "permission_denied" if status == 403 else "rate_limited")

    def test_timeout_retries_once_and_repeated_token_fails_closed(self):
        # A timed-out page or a circular cursor must not imply full coverage.
        count = 0

        def timeout_handler(request):
            nonlocal count
            count += 1
            raise httpx.ReadTimeout("late")

        with httpx.Client(transport=httpx.MockTransport(timeout_handler)) as client:
            with self.assertRaises(AlpacaBarsError) as caught:
                self.request(client)
        self.assertEqual(count, 2)
        self.assertEqual(caught.exception.kind, "timeout")

        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "bars": {}, "next_page_token": "again"}))) as client:
            with self.assertRaises(AlpacaBarsError) as caught:
                self.request(client)
        self.assertEqual(caught.exception.kind, "protocol_error")

    def test_network_and_server_failures_retry_boundedly(self):
        # A transient transport failure must not be mistaken for missing bars.
        for first in ("connection", 503):
            count = 0

            def handler(request):
                nonlocal count
                count += 1
                if count == 1:
                    if first == "connection":
                        raise httpx.ConnectError("connection lost")
                    return httpx.Response(first)
                return httpx.Response(200, json={"bars": {"APP": [
                    bar("2026-10-02T12:01:00Z", 12)]}, "next_page_token": None})

            with self.subTest(first=first):
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = self.request(client)
                self.assertEqual(count, 2)
                self.assertEqual(result["symbols"]["APP"]["observed_volume"], 12)


if __name__ == "__main__":
    unittest.main()
