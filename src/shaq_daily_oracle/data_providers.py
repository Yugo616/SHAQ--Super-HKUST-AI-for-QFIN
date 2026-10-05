from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
from html.parser import HTMLParser
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urljoin, unquote, urldefrag
from zoneinfo import ZoneInfo

from .hashing import sha256_file, sha256_payload
from .data_retry import failure_diagnostic, failure_message, is_transient_diagnostic
from .research_progress import safe_observe


class DataProviderError(ValueError):
    """A research data provider failed without permission to fabricate a substitute."""

    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


CAPABILITIES = {
    "instrument_identity",
    "pit_universe",
    "daily_bars",
    "premarket_quotes",
    "market_and_sector_bars",
    "primary_events",
    "option_surface",
    "order_flow",
    "option_trade_flow",
}


@dataclass(frozen=True)
class DataProfile:
    profile_id: str
    universe_file: str
    metadata_provider: str = "financedatabase"
    market_provider: str = "yfinance"
    event_provider: str = "sec-edgar"
    openbb_base_url: str = ""
    openbb_routes: dict[str, str] = field(default_factory=dict)
    request_timeout_seconds: int = 30
    batch_size: int = 80
    intraday_interval: str = "5m"
    maximum_candidates: int = 8
    maximum_event_characters: int = 60_000
    maximum_event_exhibits: int = 6
    maximum_option_expiries: int = 3
    maximum_option_contracts_per_side: int = 40
    occ_open_interest_enabled: bool = False
    alpaca_premarket_enabled: bool = False
    alpaca_data_base_url: str = 'https://data.alpaca.markets'
    alpaca_sip_delay_seconds: int = 900
    alpaca_safety_margin_seconds: int = 60
    alpaca_orderflow_enabled: bool = False
    alpaca_quote_window_seconds: int = 120
    alpaca_quote_max_pages: int = 10
    alpaca_quote_max_bytes: int = 5_000_000
    alpaca_quote_attempts: int = 2
    alpaca_quote_session_fallback: bool = True
    alpaca_quote_retry_backoff_seconds: float = 1.0
    alpaca_quote_retry_wait_cap_seconds: float = 30.0
    yahoo_worker_timeout_seconds: int = 900
    yahoo_request_max_retries: int = 1
    yahoo_retry_backoff_seconds: float = 0.25

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DataProfile":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unexpected = set(value) - allowed
        if unexpected:
            raise DataProviderError(
                "data profile contains unknown fields: " + ", ".join(sorted(unexpected))
            )
        profile = cls(**value)
        profile.validate()
        return profile

    def validate(self) -> None:
        if any(type(value) is not bool for value in (self.occ_open_interest_enabled, self.alpaca_premarket_enabled, self.alpaca_orderflow_enabled, self.alpaca_quote_session_fallback)):
            raise DataProviderError("supplemental provider switches must be booleans")
        if any(type(value) is not int or value <= 0 for value in (self.alpaca_quote_window_seconds,
                self.alpaca_quote_max_pages, self.alpaca_quote_max_bytes, self.alpaca_quote_attempts)):
            raise DataProviderError('Alpaca quote collection limits must be positive integers')
        if self.alpaca_quote_attempts > 3:
            raise DataProviderError('Alpaca quote attempts cannot exceed three')
        if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0
               for v in (self.alpaca_quote_retry_backoff_seconds, self.alpaca_quote_retry_wait_cap_seconds)):
            raise DataProviderError('Alpaca retry waits must be finite and nonnegative')
        endpoint = urlparse(self.alpaca_data_base_url)
        if (endpoint.scheme != 'https' or endpoint.netloc != 'data.alpaca.markets'
                or endpoint.path not in {'', '/'} or endpoint.query or endpoint.fragment):
            raise DataProviderError('Alpaca credentials are restricted to the official data origin')
        if (type(self.alpaca_sip_delay_seconds) is not int or self.alpaca_sip_delay_seconds < 900
                or type(self.alpaca_safety_margin_seconds) is not int or self.alpaca_safety_margin_seconds < 0):
            raise DataProviderError('Alpaca Basic SIP requires at least fifteen minutes of delay')
        if not self.profile_id.strip():
            raise DataProviderError("data profile id is required")
        if self.metadata_provider not in {"financedatabase", "none", "openbb-rest"}:
            raise DataProviderError("unsupported metadata provider")
        if self.market_provider not in {"yfinance", "openbb-rest"}:
            raise DataProviderError("unsupported market provider")
        if self.event_provider not in {"sec-edgar", "openbb-rest"}:
            raise DataProviderError("unsupported event provider")
        if "openbb-rest" in {
            self.metadata_provider, self.market_provider, self.event_provider
        }:
            parsed = urlparse(self.openbb_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise DataProviderError("OpenBB REST requires an absolute base URL")
            required = set()
            if self.metadata_provider == "openbb-rest":
                required.add("instrument_identity")
            if self.market_provider == "openbb-rest":
                required.update({"daily_bars", "premarket_quotes", "option_surface"})
            if self.event_provider == "openbb-rest":
                required.add("primary_events")
            if not required.issubset(self.openbb_routes):
                raise DataProviderError("OpenBB REST capability routes are incomplete")
            for capability, path in self.openbb_routes.items():
                pure = Path(path)
                if capability not in CAPABILITIES or not path.startswith("/") or ".." in pure.parts:
                    raise DataProviderError("OpenBB REST capability route is invalid")
        if self.request_timeout_seconds <= 0 or self.batch_size <= 0:
            raise DataProviderError("provider timeouts and batch size must be positive")
        if self.yahoo_worker_timeout_seconds <= 0:
            raise DataProviderError("Yahoo worker deadline must be positive")
        if (type(self.yahoo_request_max_retries) is not int
                or not 0 <= self.yahoo_request_max_retries <= 3):
            raise DataProviderError("Yahoo request retries must be an integer from 0 to 3")
        if (type(self.yahoo_retry_backoff_seconds) not in {int, float}
                or not math.isfinite(self.yahoo_retry_backoff_seconds)
                or not 0 <= self.yahoo_retry_backoff_seconds <= 5):
            raise DataProviderError("Yahoo retry backoff must be between 0 and 5 seconds")
        if self.maximum_candidates <= 0:
            raise DataProviderError("maximum_candidates must be positive")
        if self.maximum_event_characters <= 0:
            raise DataProviderError("maximum_event_characters must be positive")
        if type(self.maximum_event_exhibits) is not int or not 0 <= self.maximum_event_exhibits <= 20:
            raise DataProviderError("maximum_event_exhibits must be between 0 and 20")
        if self.maximum_option_expiries <= 0:
            raise DataProviderError("maximum_option_expiries must be positive")
        if self.maximum_option_contracts_per_side <= 0:
            raise DataProviderError("maximum_option_contracts_per_side must be positive")
        if self.intraday_interval not in {"1m", "2m", "5m", "15m", "30m", "60m"}:
            raise DataProviderError("unsupported intraday interval")

    def source_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # A process deadline changes execution, never the source/data identity.
        value.pop('yahoo_worker_timeout_seconds')
        value.pop('yahoo_request_max_retries')
        value.pop('yahoo_retry_backoff_seconds')
        return value

    def identity(self) -> str:
        return sha256_payload(self.source_dict())

    def history_identity(self) -> str:
        """Cache raw daily bars by upstream semantics, not screening/runtime knobs."""
        source = {'schema_version': 1, 'provider': self.market_provider,
                  'interval': '1d', 'prepost': False, 'adjustment': 'unadjusted'}
        if self.market_provider == 'openbb-rest':
            source.update(base_url=self.openbb_base_url,
                          route=self.openbb_routes.get('daily_bars'))
        return sha256_payload(source)


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    company_name: str
    gics_sector: str
    gics_sub_industry: str
    cik: str
    known_from_utc: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def load_versioned_universe(path: Path, *, cutoff: datetime) -> list[UniverseMember]:
    if not path.is_file():
        raise DataProviderError(f"versioned universe is missing: {path.name}")
    members: list[UniverseMember] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("instrument") or row.get("ticker") or "").strip().upper()
            if not symbol:
                continue
            known = str(row.get("known_from_utc") or "").strip()
            known_time = _parse_timestamp(known)
            if known_time is not None and known_time > cutoff.astimezone(known_time.tzinfo):
                continue
            cik = str(row.get("cik_company_id") or row.get("cik") or "").strip()
            if cik.upper().startswith("CIK:"):
                cik = cik.split(":", 1)[1]
            members.append(UniverseMember(
                symbol=symbol,
                company_name=str(row.get("company_name") or "").strip(),
                gics_sector=str(row.get("gics_sector") or "").strip(),
                gics_sub_industry=str(row.get("gics_sub_industry") or "").strip(),
                cik="".join(character for character in cik if character.isdigit()).zfill(10)
                if cik else "",
                known_from_utc=known,
            ))
    unique = {member.symbol: member for member in members}
    if not unique:
        raise DataProviderError("versioned universe is empty at the requested cutoff")
    if len(unique) != len(members):
        raise DataProviderError("versioned universe contains duplicate symbols")
    return [unique[symbol] for symbol in sorted(unique)]


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def _serialize_index(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _positive_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _option_quality(*, source_contract_count: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_price_pairs = sum(
        _positive_number(row.get("bid"))
        and _positive_number(row.get("ask"))
        and float(row["ask"]) >= float(row["bid"])
        for row in rows
    )
    retained_count = len(rows)
    if retained_count and valid_price_pairs == retained_count:
        price_pair_status = "complete"
    elif valid_price_pairs:
        price_pair_status = "partial"
    else:
        price_pair_status = "unavailable"
    return {
        "source_contract_count": source_contract_count,
        "retained_contract_count": retained_count,
        "contracts_with_valid_two_sided_price": valid_price_pairs,
        "contracts_with_positive_volume": sum(
            _positive_number(row.get("volume")) for row in rows
        ),
        "contracts_with_positive_open_interest": sum(
            _positive_number(row.get("openInterest")) for row in rows
        ),
        "price_pair_status": price_pair_status,
        "quote_freshness_status": "unverifiable",
        "contracts_with_fresh_quote": None,
    }


def _frame_rows(frame: Any, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Normalize the two yfinance column layouts into stable per-symbol records."""

    if frame is None or getattr(frame, "empty", True):
        return {symbol: [] for symbol in symbols}
    output = {symbol: [] for symbol in symbols}
    columns = frame.columns
    multi = getattr(columns, "nlevels", 1) > 1
    reverse = {_yahoo_symbol(symbol): symbol for symbol in symbols}
    for index, row in frame.iterrows():
        if multi:
            level0 = set(str(value) for value in columns.get_level_values(0))
            ticker_first = bool(level0 & set(reverse))
            for provider_symbol, original in reverse.items():
                try:
                    series = row[provider_symbol] if ticker_first else row.xs(
                        provider_symbol, level=1
                    )
                except (KeyError, TypeError):
                    continue
                record = {str(key).lower().replace(" ", "_"): value for key, value in series.items()}
                if any(value == value for value in record.values()):
                    output[original].append({"timestamp": _serialize_index(index), **record})
        else:
            original = symbols[0]
            record = {str(key).lower().replace(" ", "_"): value for key, value in row.items()}
            output[original].append({"timestamp": _serialize_index(index), **record})
    for records in output.values():
        for record in records:
            for key, value in list(record.items()):
                if key == "timestamp":
                    continue
                try:
                    if value != value:
                        record[key] = None
                    elif hasattr(value, "item"):
                        record[key] = value.item()
                except Exception:
                    record[key] = str(value)
    return output


class YFinanceProvider:
    capabilities = {
        "daily_bars", "premarket_quotes", "market_and_sector_bars", "option_surface"
    }

    def __init__(self, profile: DataProfile, progress_observer=None) -> None:
        self.profile = profile
        self.history_checkpoint_root = None
        self.progress_observer = progress_observer

    def _request_with_retry(self, request, *, stage, symbol=None):
        # Validate even directly constructed profiles before issuing any request.
        self.profile.validate()
        max_attempts = self.profile.yahoo_request_max_retries + 1
        safe_symbol = None
        if self.progress_observer is not None and symbol is not None:
            from .data_retry import sanitize_diagnostic
            safe_symbol = sanitize_diagnostic({'symbol': symbol}).get('symbol')
        for attempt in range(max_attempts):
            try:
                return request()
            except Exception as exc:
                diagnostic = failure_diagnostic(exc, stage)
                if diagnostic['kind'] == 'no_data':
                    raise
                if self.progress_observer is not None:
                    progress = dict(source='yfinance', request_stage=stage,
                                    attempt=attempt + 1, max_attempts=max_attempts,
                                    failure_kind=diagnostic['kind'])
                    if safe_symbol:
                        progress['symbol'] = safe_symbol
                    safe_observe(self.progress_observer, stage='data_request_failed', **progress)
                if (not is_transient_diagnostic(diagnostic)
                        or attempt >= self.profile.yahoo_request_max_retries):
                    diagnostic.update(attempts=attempt + 1, retry_count=attempt)
                    raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic) from exc
                delay = self.profile.yahoo_retry_backoff_seconds * (attempt + 1)
                if self.progress_observer is not None:
                    retry = {**progress, 'attempt': attempt + 2,
                             'next_retry_at': (datetime.now(timezone.utc)
                                               + timedelta(seconds=delay)).isoformat()}
                    safe_observe(self.progress_observer, stage='data_retry_scheduled', **retry)
                time.sleep(delay)
                if self.progress_observer is not None:
                    safe_observe(self.progress_observer, stage='data_retry_started', **retry)

    @staticmethod
    def _module():
        try:
            import yfinance as yf  # type: ignore
        except ImportError as exc:
            raise DataProviderError("yfinance is not installed") from exc
        return yf

    def history(
        self,
        symbols: list[str],
        *,
        start: date,
        end: date,
        interval: str = "1d",
        prepost: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        from .collection_worker import call_in_worker
        return call_in_worker('history', self.profile, {
            'symbols': symbols, 'start': start.isoformat(), 'end': end.isoformat(),
            'interval': interval, 'prepost': prepost,
            'history_checkpoint_root': str(self.history_checkpoint_root) if self.history_checkpoint_root else None,
            'history_source_identity': self.profile.history_identity(),
        }, progress_observer=self.progress_observer)

    def _history_checkpoint(self, symbol, *, start, end, interval='1d', prepost=False):
        from .collection_checkpoint import HistoryCheckpoint
        return HistoryCheckpoint(
            self.history_checkpoint_root if interval == '1d' and not prepost else None,
            {'provider': getattr(self, 'history_source_identity', self.profile.history_identity()),
             'symbol': _yahoo_symbol(symbol), 'start': start.isoformat(), 'end': end.isoformat(),
             'interval': interval, 'prepost': prepost},
        )

    def recover_history(self, symbols, *, start, end):
        """Read only completed, hashed daily requests after an owned worker failed."""
        return {symbol: rows for symbol in symbols
                if (rows := self._history_checkpoint(symbol, start=start, end=end).read()) is not None}

    def _history_inline(
        self, symbols: list[str], *, start: date, end: date,
        interval: str = '1d', prepost: bool = False, session,
    ) -> dict[str, list[dict[str, Any]]]:
        from collections import Counter
        import pandas as pd
        from yfinance.exceptions import YFPricesMissingError, YFTzMissingError

        yf = self._module()
        normalized = {_yahoo_symbol(symbol): symbol for symbol in symbols}
        output = {symbol: [] for symbol in symbols}
        failures = []
        for group in _chunks(list(normalized), self.profile.batch_size):
            try:
                frames = {}
                for symbol in group:
                    checkpoint = self._history_checkpoint(symbol, start=start, end=end,
                                                          interval=interval, prepost=prepost)
                    recovered = checkpoint.read()
                    if recovered is not None:
                        output[normalized[symbol]] = recovered
                        continue
                    try:
                        # Bulk download catches all ticker errors and turns even
                        # EMFILE into empty data. Direct history preserves errors
                        # under the worker's explicit exception configuration.
                        ticker = yf.Ticker(symbol, session=session)
                        frames[symbol] = self._request_with_retry(lambda: ticker.history(
                            start=start.isoformat(), end=end.isoformat(),
                            interval=interval, prepost=prepost, auto_adjust=False,
                            actions=False, timeout=self.profile.request_timeout_seconds,
                        ), stage='history', symbol=normalized[symbol])
                        if interval == '1d' and not prepost and not frames[symbol].empty:
                            daily = frames[symbol].copy()
                            daily.index = daily.index.tz_localize(None)
                            checkpoint.save(_frame_rows(daily, [normalized[symbol]])[normalized[symbol]])
                    except (YFPricesMissingError, YFTzMissingError):
                        frames[symbol] = pd.DataFrame()
                    except DataProviderError as exc:
                        diagnostic = {**exc.diagnostic, 'symbol': symbol, 'interval': interval,
                                      'request_start': start.isoformat(), 'request_end': end.isoformat()}
                        # A shared upstream rate limit is not a missing ticker.
                        # Stop this group after its bounded retry, rather than
                        # sending the same blocked request for every constituent.
                        if diagnostic.get('kind') == 'rate_limited' or not is_transient_diagnostic(diagnostic):
                            raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic) from exc
                        failures.append(diagnostic)
                        continue
                if not frames:
                    continue
                nonempty = [frame for frame in frames.values() if frame is not None and not frame.empty]
                # Preserve download's day+ tz-naive / intraday majority-timezone
                # contract before using the existing multi-column row normalizer.
                if interval.endswith(('m', 'h')):
                    zones = Counter(str(frame.index.tz) for frame in nonempty)
                    zone = min(zones, key=lambda item: (-zones[item], item)) if zones else None
                    for frame in nonempty:
                        frame.index = frame.index.tz_convert(zone)
                else:
                    for frame in nonempty:
                        frame.index = frame.index.tz_localize(None)
                frame = pd.concat(frames, axis=1, sort=True)
            except Exception as exc:
                diagnostic = failure_diagnostic(exc, 'history')
                raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic) from exc
            group_original = [normalized[value] for value in frames]
            rows = _frame_rows(frame, group_original)
            output.update(rows)
        if failures:
            diagnostic = {**failures[0], 'completed_symbols': sum(bool(rows) for rows in output.values()),
                          'failed_symbols': len(failures)}
            raise DataProviderError(failure_message(diagnostic), diagnostic=diagnostic)
        return output

    def fresh_history(self, *args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """A fresh child has no historical-response LRU from an earlier read."""
        return YFinanceProvider(self.profile, progress_observer=self.progress_observer).history(*args, **kwargs)

    def recent_intraday(
        self, symbols: list[str], *, cutoff: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        start = cutoff.date() - timedelta(days=5)
        end = cutoff.date() + timedelta(days=1)
        rows = self.history(
            symbols,
            start=start,
            end=end,
            interval=self.profile.intraday_interval,
            prepost=True,
        )
        cutoff_et = cutoff.astimezone(ZoneInfo("America/New_York"))
        for symbol, records in rows.items():
            safe = []
            for record in records:
                observed = _parse_timestamp(str(record["timestamp"]))
                if observed is None:
                    continue
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=ZoneInfo("America/New_York"))
                if observed.astimezone(ZoneInfo("America/New_York")) <= cutoff_et:
                    safe.append(record)
            rows[symbol] = safe
        return rows

    def option_surface(self, symbol: str) -> dict[str, Any]:
        from .collection_worker import call_in_worker
        return call_in_worker('option_surface', self.profile, {'symbol': symbol},
                              progress_observer=self.progress_observer)

    def _option_surface_inline(self, symbol: str, *, session) -> dict[str, Any]:
        yf = self._module()
        ticker = yf.Ticker(_yahoo_symbol(symbol), session=session)
        expiries = list(self._request_with_retry(lambda: ticker.options, stage='option_surface', symbol=symbol) or [])
        if not expiries:
            return {
                "symbol": symbol,
                "status": "no_data",
                "captured_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "available_expiry_count": 0,
                "attempted_expiry_count": 0,
                "collected_expiry_count": 0,
                "failed_expiry_count": 0,
                "maximum_option_expiries": self.profile.maximum_option_expiries,
                "expiry_selection": "nearest_configured_expiries",
                "quality": _option_quality(source_contract_count=0, rows=[]),
                "source_contract_count": 0,
                "retained_contract_count": 0,
                "valid_two_sided_price_count": 0,
                "capture_timestamp_semantics": "provider_response_capture_not_exchange_quote_time",
                "quote_timestamp_status": "unavailable",
                "quote_freshness_eligible": False,
                "last_trade_timestamp_semantics": "contract_last_trade_not_quote_time",
                "directional_flow_semantics": False,
                "expiries": {},
            }
        output: dict[str, Any] = {}
        aggregate_rows: list[dict[str, Any]] = []
        aggregate_source_count = 0
        selected_expiries = expiries[:self.profile.maximum_option_expiries]
        for expiry in selected_expiries:
            try:
                chain = self._request_with_retry(lambda: ticker.option_chain(expiry), stage='option_surface', symbol=symbol)
            except Exception as exc:
                diagnostic = failure_diagnostic(exc, 'option_surface')
                output[expiry] = {"status": "provider_error", "error": diagnostic.get('error_type'),
                                  "diagnostic": diagnostic}
                continue
            sides = {}
            expiry_rows: list[dict[str, Any]] = []
            expiry_source_count = 0
            reference_price = None
            try:
                reference_price = float(ticker.fast_info.get("last_price"))
            except (AttributeError, TypeError, ValueError):
                pass
            for name, frame in (("calls", chain.calls), ("puts", chain.puts)):
                rows = []
                for _, row in frame.iterrows():
                    record = {}
                    for key in (
                        "contractSymbol", "strike", "bid", "ask", "lastPrice",
                        "impliedVolatility", "volume", "openInterest", "lastTradeDate",
                    ):
                        value = row.get(key)
                        if value is None or value != value:
                            record[key] = None
                        elif key == "lastTradeDate":
                            record[key] = _serialize_index(value)
                        elif hasattr(value, "item"):
                            record[key] = value.item()
                        else:
                            record[key] = value
                    rows.append(record)
                expiry_source_count += len(rows)
                if reference_price and reference_price > 0:
                    rows.sort(key=lambda item: abs(float(item.get("strike") or 0) - reference_price))
                rows = rows[: self.profile.maximum_option_contracts_per_side]
                rows.sort(key=lambda item: float(item.get("strike") or 0))
                sides[name] = rows
                expiry_rows.extend(rows)
            quality = _option_quality(
                source_contract_count=expiry_source_count, rows=expiry_rows,
            )
            output[expiry] = {"status": "collected", "quality": quality, **sides}
            aggregate_source_count += expiry_source_count
            aggregate_rows.extend(expiry_rows)
        captured_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
        collected_expiry_count = sum(
            row.get("status") == "collected" for row in output.values()
        )
        failed_expiry_count = sum(
            row.get("status") == "provider_error" for row in output.values()
        )
        quality = _option_quality(
            source_contract_count=aggregate_source_count, rows=aggregate_rows,
        )
        return {
            "symbol": symbol,
            "status": (
                "collected" if collected_expiry_count
                else ("provider_error" if failed_expiry_count else "no_data")
            ),
            "captured_at": captured_at,
            "available_expiry_count": len(expiries),
            "attempted_expiry_count": len(selected_expiries),
            "collected_expiry_count": collected_expiry_count,
            "failed_expiry_count": failed_expiry_count,
            "maximum_option_expiries": self.profile.maximum_option_expiries,
            "expiry_selection": "nearest_configured_expiries",
            "quality": quality,
            "source_contract_count": quality["source_contract_count"],
            "retained_contract_count": quality["retained_contract_count"],
            "valid_two_sided_price_count": quality["contracts_with_valid_two_sided_price"],
            "capture_timestamp_semantics": "provider_response_capture_not_exchange_quote_time",
            "quote_timestamp_status": "unavailable",
            "quote_freshness_eligible": False,
            "last_trade_timestamp_semantics": "contract_last_trade_not_quote_time",
            "directional_flow_semantics": False,
            "expiries": output,
        }


class FinanceDatabaseProvider:
    capabilities = {"instrument_identity"}
    equities_dataset_url = (
        "https://raw.githubusercontent.com/JerBouma/FinanceDatabase/"
        "main/compression/equities.bz2"
    )

    def __init__(self, *, timeout_seconds: float, cache_root: Path | None = None):
        self.timeout_seconds = timeout_seconds
        self.cache_root = cache_root
        self.receipt: dict[str, Any] = {}

    def _cached(self):
        if self.cache_root is None:
            return None
        try:
            receipt = json.loads((self.cache_root / 'source.json').read_text())
            digest = receipt['sha256']
            if (receipt['source_url'] != self.equities_dataset_url
                    or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                return None
            content = (self.cache_root / (digest + '.bz2')).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                return None
            return content, receipt
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cache(self, content, receipt):
        if self.cache_root is None:
            return
        import os
        import tempfile
        from .settings import _atomic_json
        self.cache_root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.cache_root, suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
            os.replace(name, self.cache_root / (receipt['sha256'] + '.bz2'))
            _atomic_json(self.cache_root / 'source.json', receipt, retry_windows_readers=True)
        finally:
            Path(name).unlink(missing_ok=True)

    def metadata(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.receipt = {}
        if not symbols:
            return {}
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise DataProviderError("pandas is unavailable for FinanceDatabase metadata") from exc
        try:
            from curl_cffi.requests import Session
            from io import BytesIO
            cached = self._cached()
            headers = {}
            if cached and cached[1].get('etag'):
                headers['If-None-Match'] = cached[1]['etag']
            # Non-streaming curl applies TIMEOUT_MS to the entire transfer,
            # including redirects: a slow trickle cannot reset a read timeout.
            try:
                with Session() as session:
                    response = session.get(self.equities_dataset_url,
                                           timeout=self.timeout_seconds, headers=headers,
                                           allow_redirects=True, stream=False)
                    if response.status_code != 304:
                        response.raise_for_status()
                if response.status_code == 304 and cached:
                    content, receipt = cached
                    self.receipt = {**receipt, 'cache_status': 'revalidated',
                                    'checked_at': datetime.now(timezone.utc).isoformat()}
                elif response.status_code == 200:
                    content = response.content
                    self.receipt = {'source_url': self.equities_dataset_url,
                                    'sha256': hashlib.sha256(content).hexdigest(),
                                    'etag': response.headers.get('etag'),
                                    'fetched_at': datetime.now(timezone.utc).isoformat(),
                                    'cache_status': 'downloaded'}
                else:
                    raise DataProviderError('unexpected metadata response')
            except Exception as exc:
                diagnostic = failure_diagnostic(exc, None)
                if not cached or not is_transient_diagnostic(diagnostic):
                    raise
                content, receipt = cached
                self.receipt = {**receipt, 'cache_status': 'cached_after_refresh_failure',
                                'refresh_failure': diagnostic}
            frame = pd.read_csv(
                BytesIO(content), compression="bz2", index_col=0
            )
            if frame.empty or 'name' not in frame.columns:
                raise DataProviderError('invalid FinanceDatabase table')
            frame = frame[~frame.index.astype(str).str.contains(r"\.", na=False)]
            if self.receipt['cache_status'] != 'cached_after_refresh_failure':
                try:
                    self._save_cache(content, self.receipt)
                except OSError:
                    self.receipt['cache_write_failed'] = True
        except Exception as exc:
            raise DataProviderError(
                f"FinanceDatabase metadata read failed: {type(exc).__name__}",
                diagnostic=failure_diagnostic(exc, None),
            ) from exc
        output = {}
        for symbol in symbols:
            provider_symbol = _yahoo_symbol(symbol)
            if provider_symbol not in frame.index:
                continue
            row = frame.loc[provider_symbol]
            if hasattr(row, "iloc") and getattr(row, "ndim", 1) > 1:
                row = row.iloc[0]
            record = {}
            for key, value in row.to_dict().items():
                record[str(key)] = None if value != value else value
            output[symbol] = record
        return output


def sec_exhibit_urls(source_url: str, content: bytes) -> list[str]:
    """Discover linked 99-series exhibits, confined to this SEC accession."""
    base = urlparse(source_url)
    if base.scheme != 'https' or base.netloc not in {'www.sec.gov', 'sec.gov'}:
        return []
    directory = base.path.rsplit('/', 1)[0] + '/'
    if not re.fullmatch(r'/Archives/edgar/data/\d+/\d+/', directory):
        return []

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.row, self.link = [], None, None
            self.anchors = []

        def handle_starttag(self, tag, attrs):
            if tag == 'tr':
                self.row = {'text': [], 'links': []}
            if tag == 'a':
                self.link = {'href': dict(attrs).get('href', ''), 'text': []}

        def handle_data(self, data):
            if self.row is not None:
                self.row['text'].append(data)
            if self.link is not None:
                self.link['text'].append(data)

        def handle_endtag(self, tag):
            if tag == 'a' and self.link is not None:
                self.anchors.append(self.link)
                if self.row is not None:
                    self.row['links'].append(self.link)
                self.link = None
            if tag == 'tr' and self.row is not None:
                self.rows.append(self.row)
                self.row = None

    parser = Links()
    parser.feed(content.decode('utf-8', errors='replace'))
    identified = set()
    for row in parser.rows:
        if re.search(r'(?<!\d)99(?:\.\d+)?(?!\d)', ' '.join(row['text'])):
            identified.update(id(link) for link in row['links'])
    output = []
    for link in parser.anchors:
        href = unquote(link['href']).strip()
        label = ' '.join(link['text'])
        if (id(link) not in identified and not re.search(r'\b(?:exhibit\s*99|ex[-_]?99|earnings|news\s*release)',
                label + ' ' + href, re.I)):
            continue
        if not href or href.startswith('#') or '..' in href.split('/'):
            continue
        url = urldefrag(urljoin(source_url, href))[0]
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.netloc != base.netloc or parsed.query
                or parsed.path.rsplit('/', 1)[0] + '/' != directory
                or not parsed.path.lower().endswith(('.htm', '.html', '.txt'))
                or url == urldefrag(source_url)[0]):
            continue
        if url not in output:
            output.append(url)
    return output


class SecEdgarProvider:
    capabilities = {"primary_events"}
    allowed_forms = {"8-K", "10-Q", "10-K", "6-K", "20-F"}

    metadata_url = 'https://www.sec.gov/files/company_tickers_exchange.json'

    def __init__(self, *, user_agent: str, timeout_seconds: int,
                 cache_root: Path | None = None, max_retries: int = 1,
                 retry_backoff_seconds: float = .25,
                 cache_max_age_seconds: float = 86400) -> None:
        if not user_agent.strip():
            raise DataProviderError("SEC research identity is required")
        self.user_agent = user_agent.strip()
        self.timeout_seconds = timeout_seconds
        if (not 0 <= max_retries <= 3 or retry_backoff_seconds < 0
                or cache_max_age_seconds < 0):
            raise DataProviderError('invalid metadata recovery policy')
        self.cache_root = cache_root
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.cache_max_age_seconds = cache_max_age_seconds

    @staticmethod
    def _identity_document(content):
        document = json.loads(content)
        fields = document['fields']
        if (not {'cik', 'name', 'ticker', 'exchange'}.issubset(fields)
                or not document.get('data')):
            raise DataProviderError('invalid SEC instrument identity table')
        for row in document['data']:
            if not isinstance(row, list) or len(row) != len(fields):
                raise DataProviderError('invalid SEC instrument identity row')
        return document

    def _identity_cache(self):
        if self.cache_root is None:
            return None
        try:
            receipt = json.loads((self.cache_root / 'source.json').read_text())
            digest = receipt['sha256']
            if (receipt['source_url'] != self.metadata_url or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                return None
            content = (self.cache_root / (digest + '.json')).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                return None
            document = self._identity_document(content)
            fetched = datetime.fromisoformat(receipt['fetched_at'])
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if age < 0:
                return None
            return document, receipt, age
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _identity_table(self):
        from curl_cffi.requests import Session
        from .settings import _atomic_json
        cached = self._identity_cache()
        if cached and cached[2] < self.cache_max_age_seconds:
            self.metadata_receipt = {**cached[1], 'cache_status': 'cached'}
            return cached[0]
        for attempt in range(self.max_retries + 1):
            try:
                with Session() as session:
                    response = session.get(self.metadata_url,
                        headers={'User-Agent': self.user_agent},
                        timeout=self.timeout_seconds, stream=False)
                    response.raise_for_status()
                document = self._identity_document(response.content)
                # Store the canonical JSON bytes so the cached hash is independently verifiable.
                content = (json.dumps(document, sort_keys=True, indent=2) + '\n').encode('utf-8')
                receipt = {'provider': 'sec-edgar', 'source_url': self.metadata_url,
                    'sha256': hashlib.sha256(content).hexdigest(),
                    'response_sha256': hashlib.sha256(response.content).hexdigest(),
                    'fetched_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'company_name_exchange_cik_only'}
                if self.cache_root is not None:
                    self.cache_root.mkdir(parents=True, exist_ok=True)
                    _atomic_json(self.cache_root / (receipt['sha256'] + '.json'), document,
                                 retry_windows_readers=True)
                    _atomic_json(self.cache_root / 'source.json', receipt, retry_windows_readers=True)
                self.metadata_receipt = {**receipt, 'cache_status': 'downloaded', 'attempts': attempt + 1}
                return document
            except Exception as exc:
                diagnostic = failure_diagnostic(exc, None)
                if is_transient_diagnostic(diagnostic):
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_seconds * (attempt + 1))
                        continue
                    if cached:
                        self.metadata_receipt = {**cached[1], 'cache_status': 'cached_after_refresh_failure',
                            'refresh_failure': diagnostic}
                        return cached[0]
                raise DataProviderError('SEC identity lookup failed', diagnostic=diagnostic) from exc

    def instrument_metadata(self, members: list[UniverseMember]) -> dict[str, dict[str, Any]]:
        """Official identity-only fallback; never invent sectors or relationships."""
        if not members:
            return {}
        document = self._identity_table()
        fields = document['fields']
        if not {'cik', 'name', 'ticker', 'exchange'}.issubset(fields):
            raise DataProviderError('invalid SEC instrument identity table')
        requested = {_yahoo_symbol(member.symbol): member for member in members}
        output = {}
        for values in document['data']:
            row = dict(zip(fields, values, strict=True))
            member = requested.get(_yahoo_symbol(row['ticker']))
            if member is None or not member.cik or int(member.cik) != int(row['cik']):
                continue
            output[member.symbol] = {key: row[key] for key in ('name', 'exchange', 'cik')}
        return output

    def recent_events(
        self, members: list[UniverseMember], *, cutoff: datetime, since: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise DataProviderError("httpx is unavailable") from exc
        output = {member.symbol: [] for member in members}
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        for member in members:
            if not member.cik:
                continue
            url = f"https://data.sec.gov/submissions/CIK{member.cik}.json"
            try:
                document = json.loads(self._request_sec_bytes(url, accept='application/json'))
            except Exception as exc:
                output[member.symbol] = [{
                    "status": "provider_error",
                    "provider": "sec-edgar",
                    "error_type": type(exc).__name__,
                    "diagnostic": failure_diagnostic(exc, None),
                }]
                continue
            recent = document.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            for index, form in enumerate(forms):
                if form not in self.allowed_forms:
                    continue
                accepted_values = recent.get("acceptanceDateTime", [])
                accepted_text = accepted_values[index] if index < len(accepted_values) else ""
                accepted = _parse_timestamp(str(accepted_text))
                if accepted is None:
                    output[member.symbol].append({"status": "provider_error",
                        "provider": "sec-edgar", "reason": "missing_acceptance_timestamp",
                        "form": form})
                    continue
                if accepted is None or accepted < since or accepted > cutoff:
                    continue
                accession_values = recent.get("accessionNumber", [])
                primary_values = recent.get("primaryDocument", [])
                accession = accession_values[index] if index < len(accession_values) else ""
                primary = primary_values[index] if index < len(primary_values) else ""
                accession_compact = str(accession).replace("-", "")
                source = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(member.cik)}/"
                    f"{accession_compact}/{primary}"
                    if accession and primary else url
                )
                output[member.symbol].append({
                    "status": "collected",
                    "provider": "sec-edgar",
                    "form": form,
                    "acceptance_time": accepted.isoformat(),
                    "accession_number": accession,
                    "primary_document": primary,
                    "source_url": source,
                })
        return output

    def download_primary_document(self, source_url: str) -> bytes:
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.netloc not in {"www.sec.gov", "sec.gov"}:
            raise DataProviderError("SEC primary document URL is outside sec.gov")
        return self._request_sec_bytes(source_url, accept='text/html,*/*')

    def _request_sec_bytes(self, source_url: str, *, accept: str) -> bytes:
        import httpx  # type: ignore
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.get(source_url,
                    headers={"User-Agent": self.user_agent, "Accept": accept},
                    timeout=self.timeout_seconds, follow_redirects=False)
                response.raise_for_status()
                content = response.content
                break
            except Exception as exc:
                # Some desktop proxy stacks reject httpx's CONNECT negotiation.
                # Retry the same official URL/identity with the system transport;
                # never use this path for HTTP permission/rate-limit responses.
                if isinstance(exc, httpx.ProxyError):
                    import urllib.request
                    try:
                        request = urllib.request.Request(source_url, headers={
                            'User-Agent': self.user_agent, 'Accept': accept})
                        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as fallback:
                            final_url = fallback.geturl()
                            if isinstance(final_url, str) and final_url != source_url:
                                raise DataProviderError('SEC document redirect is not permitted')
                            content = fallback.read(25_000_001)
                        break
                    except Exception as fallback_error:
                        from urllib.error import URLError, HTTPError
                        fallback_diagnostic = failure_diagnostic(fallback_error, None)
                        if isinstance(fallback_error, URLError) and not isinstance(fallback_error, HTTPError):
                            fallback_diagnostic = {'kind': 'connection_error', 'retryable': True,
                                                   'error_type': type(fallback_error).__name__}
                        if is_transient_diagnostic(fallback_diagnostic) and attempt < self.max_retries:
                            time.sleep(self.retry_backoff_seconds * (attempt + 1))
                            continue
                        raise DataProviderError('SEC document transport recovery failed',
                            diagnostic=fallback_diagnostic) from fallback_error
                diagnostic = failure_diagnostic(exc, None)
                if isinstance(exc, httpx.TimeoutException):
                    diagnostic = {"kind": "timeout", "retryable": True, "error_type": type(exc).__name__}
                elif isinstance(exc, httpx.NetworkError):
                    diagnostic = {"kind": "connection_error", "retryable": True, "error_type": type(exc).__name__}
                if is_transient_diagnostic(diagnostic) and attempt < self.max_retries:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
                    continue
                raise DataProviderError('SEC primary document download failed',
                                        diagnostic=diagnostic) from exc
        if not content or len(content) > 25_000_000:
            raise DataProviderError("SEC primary document is empty or exceeds the safety limit")
        return content


class OpenBBRestProvider:
    """Optional adapter for a separately operated OpenBB REST service."""

    def __init__(self, *, base_url: str, timeout_seconds: int, api_key: str = "") -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DataProviderError("OpenBB REST base URL is invalid")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or ".." in Path(path).parts:
            raise DataProviderError("OpenBB endpoint path is invalid")
        try:
            import httpx  # type: ignore
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            response = httpx.get(
                self.base_url + path,
                params=params or {},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DataProviderError(
                f"OpenBB REST request failed: {type(exc).__name__}: {exc}"
            ) from exc


class OpenBBProviderAdapter:
    """Normalized capability adapter for a separately operated OpenBB REST service."""

    def __init__(
        self, *, profile: DataProfile, api_key: str = "", sec_user_agent: str = ""
    ) -> None:
        self.profile = profile
        self.sec_user_agent = sec_user_agent
        self.client = OpenBBRestProvider(
            base_url=profile.openbb_base_url,
            timeout_seconds=profile.request_timeout_seconds,
            api_key=api_key,
        )

    def _results(self, capability: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        path = self.profile.openbb_routes.get(capability)
        if not path:
            raise DataProviderError(f"OpenBB route is not configured for {capability}")
        value = self.client.get(path, params=params)
        if isinstance(value, dict):
            value = value.get("results", value.get("data"))
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise DataProviderError(f"OpenBB {capability} response is not a record list")
        return value

    def history(
        self, symbols: list[str], *, start: date, end: date,
        interval: str = "1d", prepost: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        output = {}
        for symbol in symbols:
            rows = self._results("daily_bars", {
                "symbol": symbol, "start_date": start.isoformat(),
                "end_date": end.isoformat(), "interval": interval,
                "prepost": str(prepost).lower(),
            })
            output[symbol] = [
                {**row, "timestamp": row.get("timestamp") or row.get("date")}
                for row in rows
            ]
        return output

    def recent_intraday(
        self, symbols: list[str], *, cutoff: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        output = {}
        for symbol in symbols:
            rows = self._results("premarket_quotes", {
                "symbol": symbol, "date": cutoff.date().isoformat(),
                "interval": self.profile.intraday_interval,
            })
            safe = []
            for row in rows:
                normalized = {**row, "timestamp": row.get("timestamp") or row.get("date")}
                observed = _parse_timestamp(str(normalized["timestamp"]))
                if observed is not None and observed <= cutoff.astimezone(observed.tzinfo):
                    safe.append(normalized)
            output[symbol] = safe
        return output

    def option_surface(self, symbol: str) -> dict[str, Any]:
        rows = self._results("option_surface", {"symbol": symbol})
        return {
            "symbol": symbol, "status": "collected" if rows else "no_data",
            "directional_flow_semantics": False, "contracts": rows,
        }

    def metadata(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        output = {}
        for symbol in symbols:
            rows = self._results("instrument_identity", {"symbol": symbol})
            if rows:
                output[symbol] = rows[0]
        return output

    def recent_events(
        self, members: list[UniverseMember], *, cutoff: datetime, since: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        output = {}
        for member in members:
            rows = self._results("primary_events", {
                "symbol": member.symbol, "cik": member.cik,
                "start_date": since.isoformat(), "end_date": cutoff.isoformat(),
            })
            safe = []
            for row in rows:
                observed = _parse_timestamp(str(
                    row.get("acceptance_time") or row.get("published_at") or ""
                ))
                if observed is not None and since <= observed <= cutoff:
                    safe.append({"status": "collected", "provider": "openbb-rest", **row})
            output[member.symbol] = safe
        return output

    def download_primary_document(self, source_url: str) -> bytes:
        if not self.sec_user_agent:
            raise DataProviderError("SEC identity is required to download a primary filing")
        return SecEdgarProvider(
            user_agent=self.sec_user_agent,
            timeout_seconds=self.profile.request_timeout_seconds,
        ).download_primary_document(source_url)


def provider_manifest(
    *, profile: DataProfile, universe_path: Path, cutoff: datetime
) -> dict[str, Any]:
    active = {
        "pit_universe": "versioned-csv",
        "instrument_identity": profile.metadata_provider,
        "daily_bars": profile.market_provider,
        "premarket_quotes": profile.market_provider,
        "market_and_sector_bars": profile.market_provider,
        "primary_events": profile.event_provider,
        "option_surface": profile.market_provider,
        "order_flow": "alpaca-iex-delayed-sip-session-liquidity-v2" if profile.alpaca_orderflow_enabled else "unavailable",
        "option_trade_flow": "unavailable",
    }
    return {
        "schema_version": 1,
        "profile_id": profile.profile_id,
        "profile_sha256": profile.identity(),
        "cutoff_et": cutoff.astimezone(ZoneInfo("America/New_York")).isoformat(),
        "universe_file_sha256": sha256_file(universe_path),
        "capabilities": {name: active[name] for name in sorted(CAPABILITIES)},
        "free_research_data_only": True,
        "production_grade_claimed": False,
        "supplemental_sources": {
            "option_open_interest": "occ" if profile.occ_open_interest_enabled else "disabled",
            "delayed_premarket_volume": "alpaca-sip" if profile.alpaca_premarket_enabled else "disabled",
        },
        "manifest_sha256": sha256_payload({
            "profile": profile.source_dict(),
            "universe_file_sha256": sha256_file(universe_path),
            "active": active,
        }),
    }
