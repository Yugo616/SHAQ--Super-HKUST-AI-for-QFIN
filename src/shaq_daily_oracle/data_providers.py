from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .hashing import sha256_file, sha256_payload


class DataProviderError(ValueError):
    """A research data provider failed without permission to fabricate a substitute."""


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
    maximum_option_contracts_per_side: int = 40

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
        if self.maximum_candidates <= 0:
            raise DataProviderError("maximum_candidates must be positive")
        if self.maximum_event_characters <= 0:
            raise DataProviderError("maximum_event_characters must be positive")
        if self.maximum_option_contracts_per_side <= 0:
            raise DataProviderError("maximum_option_contracts_per_side must be positive")
        if self.intraday_interval not in {"1m", "2m", "5m", "15m", "30m", "60m"}:
            raise DataProviderError("unsupported intraday interval")

    def identity(self) -> str:
        return sha256_payload(asdict(self))


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

    def __init__(self, profile: DataProfile) -> None:
        self.profile = profile

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
        yf = self._module()
        normalized = {_yahoo_symbol(symbol): symbol for symbol in symbols}
        output = {symbol: [] for symbol in symbols}
        for group in _chunks(list(normalized), self.profile.batch_size):
            try:
                frame = yf.download(
                    tickers=group,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval=interval,
                    prepost=prepost,
                    auto_adjust=False,
                    actions=False,
                    group_by="ticker",
                    threads=True,
                    progress=False,
                    timeout=self.profile.request_timeout_seconds,
                )
            except Exception as exc:
                raise DataProviderError(
                    f"yfinance history request failed: {type(exc).__name__}: {exc}"
                ) from exc
            group_original = [normalized[value] for value in group]
            rows = _frame_rows(frame, group_original)
            output.update(rows)
        return output

    def fresh_history(self, *args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Bypass yfinance's process-local historical-response LRU for verification reads."""
        yf = self._module()
        try:
            yf.data.YfData.cache_get.cache_clear()
        except AttributeError as exc:
            raise DataProviderError("yfinance history cache cannot be cleared") from exc
        return self.history(*args, **kwargs)

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
        yf = self._module()
        ticker = yf.Ticker(_yahoo_symbol(symbol))
        expiries = list(ticker.options or [])
        if not expiries:
            return {"symbol": symbol, "status": "no_data", "expiries": {}}
        output: dict[str, Any] = {}
        for expiry in expiries[:3]:
            try:
                chain = ticker.option_chain(expiry)
            except Exception as exc:
                output[expiry] = {"status": "provider_error", "error": type(exc).__name__}
                continue
            sides = {}
            for name, frame in (("calls", chain.calls), ("puts", chain.puts)):
                rows = []
                for _, row in frame.iterrows():
                    record = {}
                    for key in (
                        "contractSymbol", "strike", "bid", "ask", "lastPrice",
                        "impliedVolatility", "volume", "openInterest",
                    ):
                        value = row.get(key)
                        if value is None or value != value:
                            record[key] = None
                        elif hasattr(value, "item"):
                            record[key] = value.item()
                        else:
                            record[key] = value
                    rows.append(record)
                reference_price = None
                try:
                    reference_price = float(ticker.fast_info.get("last_price"))
                except (AttributeError, TypeError, ValueError):
                    pass
                if reference_price and reference_price > 0:
                    rows.sort(key=lambda item: abs(float(item.get("strike") or 0) - reference_price))
                rows = rows[: self.profile.maximum_option_contracts_per_side]
                rows.sort(key=lambda item: float(item.get("strike") or 0))
                sides[name] = rows
            output[expiry] = {"status": "collected", **sides}
        return {
            "symbol": symbol,
            "status": "collected" if output else "no_data",
            "directional_flow_semantics": False,
            "expiries": output,
        }


class FinanceDatabaseProvider:
    capabilities = {"instrument_identity"}
    equities_dataset_url = (
        "https://raw.githubusercontent.com/JerBouma/FinanceDatabase/"
        "main/compression/equities.bz2"
    )

    def metadata(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise DataProviderError("pandas is unavailable for FinanceDatabase metadata") from exc
        try:
            frame = pd.read_csv(
                self.equities_dataset_url, compression="bz2", index_col=0
            )
            frame = frame[~frame.index.astype(str).str.contains(r"\.", na=False)]
        except Exception as exc:
            raise DataProviderError(
                f"FinanceDatabase metadata read failed: {type(exc).__name__}: {exc}"
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


class SecEdgarProvider:
    capabilities = {"primary_events"}
    allowed_forms = {"8-K", "10-Q", "10-K", "6-K", "20-F"}

    def __init__(self, *, user_agent: str, timeout_seconds: int) -> None:
        if not user_agent.strip():
            raise DataProviderError("SEC research identity is required")
        self.user_agent = user_agent.strip()
        self.timeout_seconds = timeout_seconds

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
                response = httpx.get(url, headers=headers, timeout=self.timeout_seconds)
                response.raise_for_status()
                document = response.json()
            except Exception as exc:
                output[member.symbol] = [{
                    "status": "provider_error",
                    "provider": "sec-edgar",
                    "error_type": type(exc).__name__,
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
                    filing_dates = recent.get("filingDate", [])
                    filing = filing_dates[index] if index < len(filing_dates) else ""
                    accepted = _parse_timestamp(str(filing) + "T00:00:00-05:00")
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
        try:
            import httpx  # type: ignore

            response = httpx.get(
                source_url,
                headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*"},
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
            content = response.content
        except Exception as exc:
            raise DataProviderError(
                f"SEC primary document download failed: {type(exc).__name__}: {exc}"
            ) from exc
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
        "order_flow": "unavailable",
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
        "manifest_sha256": sha256_payload({
            "profile": asdict(profile),
            "universe_file_sha256": sha256_file(universe_path),
            "active": active,
        }),
    }
