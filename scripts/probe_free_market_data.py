#!/usr/bin/env python3
"""Capture no-key market-data diagnostics outside the repository.

This is a reproducibility probe, not a production data-source endorsement.  It
stores exact HTTP response bodies and a hash manifest.  It never reads API-key
environment variables and refuses to place evidence inside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
USER_AGENT = "SHAQ-data-quality-probe/1.0"


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def summarize_yahoo_chart(payload: dict[str, Any], session_date: date) -> dict[str, Any]:
    """Summarize only 04:00 through 08:50 ET without filling missing volume."""

    window_start = datetime.combine(session_date, time(4, 0), tzinfo=ET)
    window_end = datetime.combine(session_date, time(8, 50), tzinfo=ET)
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    result = results[0] if isinstance(results, list) and results else {}
    timestamps = result.get("timestamp") if isinstance(result, dict) else []
    indicators = result.get("indicators") if isinstance(result, dict) else {}
    quotes = indicators.get("quote") if isinstance(indicators, dict) else []
    quote = quotes[0] if isinstance(quotes, list) and quotes else {}
    volumes = quote.get("volume") if isinstance(quote, dict) else []

    eligible: list[tuple[datetime, Any]] = []
    for index, stamp in enumerate(timestamps or []):
        try:
            observed = datetime.fromtimestamp(float(stamp), tz=timezone.utc).astimezone(ET)
        except (TypeError, ValueError, OverflowError):
            continue
        if window_start <= observed <= window_end:
            volume = volumes[index] if isinstance(volumes, list) and index < len(volumes) else None
            eligible.append((observed, volume))

    present = [float(value) for _, value in eligible if _numeric(value) and float(value) >= 0]
    missing_count = len(eligible) - len(present)
    positive_count = sum(value > 0 for value in present)
    zero_count = sum(value == 0 for value in present)
    if not eligible:
        status = "unavailable"
    elif missing_count == len(eligible):
        status = "missing"
    elif missing_count:
        status = "partially_missing"
    elif positive_count:
        status = "observed_positive"
    else:
        status = "provider_reported_zero"
    return {
        "timezone": "America/New_York",
        "sampling_window_et": f"{window_start.isoformat()}/{window_end.isoformat()}",
        "window_end_is_inclusive": True,
        "eligible_bar_count": len(eligible),
        "first_observation_et": eligible[0][0].isoformat() if eligible else None,
        "last_observation_et": eligible[-1][0].isoformat() if eligible else None,
        "volume_observation_count": len(present),
        "positive_volume_bar_count": positive_count,
        "zero_volume_bar_count": zero_count,
        "missing_volume_bar_count": missing_count,
        "observed_volume": sum(present) if present else None,
        "volume_status": status,
        "volume_ranking_eligible": status == "observed_positive",
        "interpretation_limit": (
            "Unofficial endpoint diagnostic only; does not establish production licensing, "
            "consolidated-market coverage, or provider reliability."
        ),
    }


def prepare_run_directory(
    output_root: Path,
    *,
    symbol: str,
    session_date: date,
    captured_at: datetime,
    repository_root: Path | None = None,
) -> Path:
    repository = (repository_root or Path(__file__).resolve().parents[1]).resolve()
    output = output_root.expanduser().resolve()
    if output == repository or repository in output.parents:
        raise ValueError("probe evidence must be written outside the repository")
    run_name = f"{captured_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{symbol}-{session_date.isoformat()}"
    run_directory = output / run_name
    run_directory.mkdir(parents=True, exist_ok=False)
    return run_directory


def _request(url: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"content-type", "content-length", "date", "server", "x-request-id"}
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _write_raw(run_directory: Path, name: str, body: bytes) -> dict[str, Any]:
    path = run_directory / name
    path.write_bytes(body)
    return {
        "file": name,
        "byte_count": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _window_query(session_date: date) -> tuple[str, str]:
    start = datetime.combine(session_date, time(4, 0), tzinfo=ET)
    end = datetime.combine(session_date, time(9, 31), tzinfo=ET)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def run_probe(symbol: str, session_date: date, output_root: Path) -> Path:
    captured_at = datetime.now(ET)
    run_directory = prepare_run_directory(
        output_root,
        symbol=symbol,
        session_date=session_date,
        captured_at=captured_at,
    )
    period_start = datetime.combine(session_date, time(4, 0), tzinfo=ET)
    period_end = datetime.combine(session_date + timedelta(days=1), time(0, 0), tzinfo=ET)
    chart_query = urllib.parse.urlencode({
        "period1": int(period_start.timestamp()),
        "period2": int(period_end.timestamp()),
        "interval": "5m",
        "includePrePost": "true",
        "events": "div,splits",
    })
    chart_url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?"
        f"{chart_query}"
    )
    chart_status, chart_headers, chart_body = _request(chart_url)
    chart_record: dict[str, Any] = {
        "provider": "Yahoo Finance chart web endpoint",
        "request_url": chart_url,
        "http_status": chart_status,
        "response_headers": _safe_headers(chart_headers),
        "credentials_used": False,
        "response_kind": "exact_http_body",
        **_write_raw(run_directory, "yahoo-chart.response", chart_body),
    }
    if chart_status == 200:
        try:
            chart_record["summary"] = summarize_yahoo_chart(
                json.loads(chart_body.decode("utf-8")), session_date,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            chart_record["parse_error"] = f"{type(exc).__name__}: {exc}"

    start_iso, end_iso = _window_query(session_date)
    alpaca_query = urllib.parse.urlencode({
        "timeframe": "5Min",
        "start": start_iso,
        "end": end_iso,
        "feed": "iex",
        "adjustment": "raw",
        "limit": 10000,
    })
    alpaca_url = (
        f"https://data.alpaca.markets/v2/stocks/{urllib.parse.quote(symbol)}/bars?"
        f"{alpaca_query}"
    )
    alpaca_status, alpaca_headers, alpaca_body = _request(alpaca_url)
    alpaca_record = {
        "provider": "Alpaca Market Data API",
        "request_url": alpaca_url,
        "http_status": alpaca_status,
        "response_headers": _safe_headers(alpaca_headers),
        "credentials_used": False,
        "response_kind": "exact_http_body",
        "expected_without_credentials": "authentication_required",
        **_write_raw(run_directory, "alpaca-bars.response", alpaca_body),
    }

    manifest = {
        "schema_version": 1,
        "probe_purpose": "no-key premarket-volume source diagnostic",
        "symbol": symbol,
        "session_date": session_date.isoformat(),
        "captured_at_et": captured_at.isoformat(),
        "requested_sampling_window_et": (
            f"{session_date.isoformat()}T04:00:00 through "
            f"{session_date.isoformat()}T08:50:00 America/New_York, inclusive"
        ),
        "credentials_read_or_used": False,
        "production_approval": False,
        "sources": [chart_record, alpaca_record],
    }
    (run_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="US equity symbol, for example VRT")
    parser.add_argument("--session-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path,
                        help="external evidence root; paths inside this repository are rejected")
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    if not symbol or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ.-" for character in symbol):
        parser.error("symbol must contain only A-Z, dot, or hyphen")
    run_directory = run_probe(symbol, args.session_date, args.output)
    print(run_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
