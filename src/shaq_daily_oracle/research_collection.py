from __future__ import annotations

import csv
import json
import math
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .data_providers import (
    DataProfile,
    FinanceDatabaseProvider,
    OpenBBProviderAdapter,
    SecEdgarProvider,
    YFinanceProvider,
    load_versioned_universe,
    provider_manifest,
)
from .hashing import sha256_payload
from .market_calendar import market_session, previous_market_session
from .research_batch import FrozenEvidence, freeze_evidence_bundle


class ResearchCollectionError(ValueError):
    """The cross-platform research evidence snapshot cannot be formed safely."""


ET = ZoneInfo("America/New_York")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


def _document_text(content: bytes, maximum_characters: int) -> str:
    parser = _VisibleText()
    parser.feed(content.decode("utf-8", errors="replace"))
    return "\n".join(parser.parts)[:maximum_characters]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(
        value, indent=2, sort_keys=True, ensure_ascii=False, default=str
    ) + "\n").encode("utf-8")


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        observed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=ET)
    return observed.astimezone(ET)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sorted(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda row: str(row.get("timestamp", "")))


def _daily_state(records: list[dict[str, Any]], session_date: date) -> dict[str, Any]:
    eligible = []
    for row in _sorted(records):
        observed = _timestamp(row.get("timestamp"))
        close = _number(row.get("close"))
        if observed is not None and observed.date() < session_date and close is not None:
            eligible.append((observed, row, close))
    closes = [item[2] for item in eligible]
    return {
        "previous_close": closes[-1] if closes else None,
        "previous_return": (
            closes[-1] / closes[-2] - 1 if len(closes) >= 2 and closes[-2] else None
        ),
        "bars": [item[1] for item in eligible[-252:]],
    }


def _premarket_state(
    records: list[dict[str, Any]], *, session_date: date, cutoff: datetime,
    previous_close: float | None,
) -> dict[str, Any]:
    eligible = []
    for row in _sorted(records):
        observed = _timestamp(row.get("timestamp"))
        close = _number(row.get("close"))
        if (
            observed is not None and observed.date() == session_date
            and time(4, 0) <= observed.time().replace(tzinfo=None) <= time(8, 50)
            and observed <= cutoff and close is not None
        ):
            eligible.append((observed, row, close))
    last_price = eligible[-1][2] if eligible else None
    volume = sum(_number(item[1].get("volume")) or 0.0 for item in eligible)
    return {
        "status": "collected" if eligible else "no_data",
        "first_observation_et": eligible[0][0].isoformat() if eligible else None,
        "last_observation_et": eligible[-1][0].isoformat() if eligible else None,
        "last_price": last_price,
        "previous_close": previous_close,
        "premarket_return": (
            last_price / previous_close - 1
            if last_price is not None and previous_close not in {None, 0}
            else None
        ),
        "observed_volume": volume,
        "bars": [item[1] for item in eligible],
    }


def _benchmark_rows(path: Path) -> tuple[list[str], dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    symbols = [str(row["instrument"]).strip().upper() for row in rows]
    sector = {
        str(row["gics_sector"]).strip(): str(row["instrument"]).strip().upper()
        for row in rows if str(row.get("gics_sector", "")).strip()
    }
    if len(symbols) != len(set(symbols)) or not sector:
        raise ResearchCollectionError("market benchmark configuration is invalid")
    return symbols, sector


def _candidate_rows(
    *,
    members: list[Any],
    stock_daily: dict[str, list[dict[str, Any]]],
    stock_intraday: dict[str, list[dict[str, Any]]],
    benchmark_daily: dict[str, list[dict[str, Any]]],
    benchmark_intraday: dict[str, list[dict[str, Any]]],
    sector_etf: dict[str, str],
    session_date: date,
    cutoff: datetime,
    maximum_candidates: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    stock_states: dict[str, dict[str, Any]] = {}
    benchmark_states: dict[str, dict[str, Any]] = {}
    for symbol, records in benchmark_daily.items():
        daily = _daily_state(records, session_date)
        benchmark_states[symbol] = {
            "daily": daily,
            "premarket": _premarket_state(
                benchmark_intraday.get(symbol, []), session_date=session_date,
                cutoff=cutoff, previous_close=daily["previous_close"],
            ),
        }
    ranked = []
    for member in members:
        daily = _daily_state(stock_daily.get(member.symbol, []), session_date)
        premarket = _premarket_state(
            stock_intraday.get(member.symbol, []), session_date=session_date,
            cutoff=cutoff, previous_close=daily["previous_close"],
        )
        stock_states[member.symbol] = {"daily": daily, "premarket": premarket}
        benchmark = sector_etf.get(member.gics_sector)
        benchmark_state = benchmark_states.get(benchmark or "", {})
        stock_gap = premarket.get("premarket_return")
        sector_gap = benchmark_state.get("premarket", {}).get("premarket_return")
        stock_prior = daily.get("previous_return")
        sector_prior = benchmark_state.get("daily", {}).get("previous_return")
        if stock_gap is not None and sector_gap is not None:
            metric = abs(stock_gap - sector_gap)
            method = "premarket_stock_minus_sector_absolute_residual"
        elif stock_prior is not None and sector_prior is not None:
            metric = abs(stock_prior - sector_prior)
            method = "t_minus_1_stock_minus_sector_absolute_residual"
        else:
            continue
        ranked.append((
            -metric, -float(premarket.get("observed_volume") or 0), member.symbol,
            {
                "symbol": member.symbol,
                "company_name": member.company_name,
                "gics_sector": member.gics_sector,
                "gics_sub_industry": member.gics_sub_industry,
                "sector_benchmark": benchmark,
                "selection_method": method,
                "selection_metric": metric,
                "premarket_return": stock_gap,
                "sector_premarket_return": sector_gap,
                "captured_primary_event": False,
            },
        ))
    candidates = [row[3] for row in sorted(ranked)[:maximum_candidates]]
    if not candidates:
        raise ResearchCollectionError("free research providers produced no eligible candidates")
    return candidates, stock_states, benchmark_states


def collect_research_evidence(
    *,
    root: Path,
    package_root: Path,
    profile: DataProfile,
    sec_identity: str,
    observed_at: datetime | None = None,
    market_provider: Any | None = None,
    metadata_provider: Any | None = None,
    event_provider: Any | None = None,
    openbb_api_key: str = "",
    screening_rules: dict[str, str] | None = None,
    history_cache_root: Path | None = None,
    allow_replay: bool = False,
) -> FrozenEvidence:
    now = (observed_at or datetime.now(ET)).astimezone(ET)
    session = market_session(now.date())
    if session is None:
        if not allow_replay:
            raise ResearchCollectionError("today_is_not_a_nyse_trading_session")
        session = previous_market_session(now.date())
    scheduled_cutoff = datetime.combine(session.session_date, time(8, 50), ET)
    data_cutoff = min(now, scheduled_cutoff)
    if not allow_replay and data_cutoff < datetime.combine(session.session_date, time(4, 0), ET):
        raise ResearchCollectionError("premarket_collection_has_not_started")
    universe_path = Path(profile.universe_file)
    if not universe_path.is_absolute():
        universe_path = package_root / universe_path
    members = load_versioned_universe(universe_path, cutoff=data_cutoff)
    benchmark_path = package_root / "config/market-benchmarks.csv"
    benchmark_symbols, sector_etf = _benchmark_rows(benchmark_path)
    openbb = OpenBBProviderAdapter(
        profile=profile, api_key=openbb_api_key,
        sec_user_agent=sec_identity,
    ) if "openbb-rest" in {
        profile.market_provider, profile.metadata_provider, profile.event_provider
    } else None
    market = market_provider or (
        openbb if profile.market_provider == "openbb-rest" else YFinanceProvider(profile)
    )
    public_config_path = package_root / "config/public-data.json"
    public_config = json.loads(public_config_path.read_text(encoding="utf-8")) if public_config_path.exists() else None
    if history_cache_root is not None and public_config:
        from .public_data import DailyBarCache
        market = DailyBarCache(market, history_cache_root / profile.identity(), overlap_days=public_config["history_overlap_days"])
    lookback_start = session.session_date - timedelta(days=400)
    stock_symbols = [member.symbol for member in members]
    stock_daily = market.history(
        stock_symbols, start=lookback_start, end=session.session_date, interval="1d"
    )
    benchmark_daily = market.history(
        benchmark_symbols, start=lookback_start, end=session.session_date, interval="1d"
    )
    stock_intraday = market.recent_intraday(stock_symbols, cutoff=data_cutoff)
    benchmark_intraday = market.recent_intraday(benchmark_symbols, cutoff=data_cutoff)
    candidates, stock_states, benchmark_states = _candidate_rows(
        members=members, stock_daily=stock_daily, stock_intraday=stock_intraday,
        benchmark_daily=benchmark_daily, benchmark_intraday=benchmark_intraday,
        sector_etf=sector_etf, session_date=session.session_date, cutoff=data_cutoff,
        maximum_candidates=len(members) if screening_rules else profile.maximum_candidates,
    )
    candidate_sets = {}
    full_pool = list(candidates)
    if screening_rules:
        from .module_rules import select_symbols
        for identity, script in sorted(screening_rules.items()):
            candidate_sets[identity] = select_symbols(script, full_pool, profile.maximum_candidates)
        selected = {s for symbols in candidate_sets.values() for s in symbols}
        candidates = [c for c in full_pool if c["symbol"] in selected]
    candidate_symbols = [row["symbol"] for row in candidates]
    member_by_symbol = {member.symbol: member for member in members}
    metadata: dict[str, Any] = {}
    metadata_status = "not_configured"
    if profile.metadata_provider == "financedatabase":
        try:
            metadata = (metadata_provider or FinanceDatabaseProvider()).metadata(candidate_symbols)
            metadata_status = "collected"
        except Exception as exc:
            metadata_status = f"provider_error:{type(exc).__name__}"
    elif profile.metadata_provider == "openbb-rest":
        try:
            metadata = (metadata_provider or openbb).metadata(candidate_symbols)
            metadata_status = "collected"
        except Exception as exc:
            metadata_status = f"provider_error:{type(exc).__name__}"
    previous = previous_market_session(session.session_date)
    events: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in candidate_symbols}
    sec = event_provider or (
        openbb if profile.event_provider == "openbb-rest" else SecEdgarProvider(
            user_agent=sec_identity, timeout_seconds=profile.request_timeout_seconds
        )
    )
    try:
        events = sec.recent_events(
            [member_by_symbol[symbol] for symbol in candidate_symbols],
            cutoff=data_cutoff, since=previous.market_close,
        )
    except Exception as exc:
        events = {
            symbol: [{"status": "provider_error", "error_type": type(exc).__name__}]
            for symbol in candidate_symbols
        }
    files: dict[str, bytes] = {}
    if screening_rules:
        files["raw/screening.json"] = _json_bytes({"pool": full_pool, "candidate_sets": candidate_sets})
    records: list[dict[str, Any]] = []
    public_statuses = []
    if public_config and market_provider is None:
        from .public_data import collect_public_context
        for packet in collect_public_context(public_config, cutoff=scheduled_cutoff):
            name = packet["provider"]
            raw = packet.pop("raw", None)
            public_statuses.append({k: v for k, v in packet.items() if k != "data"})
            if raw is not None:
                files[f"raw/public/{name}.source"] = raw
            files[f"raw/public/{name}.json"] = _json_bytes(packet)
            if packet["status"] == "collected" and packet.get("data"):
                records.append({"evidence_id": "ev_public_"+name, "domain": "market", "provider": name,
                    "source_uri": packet["source_uri"], "captured_at": packet["captured_at"],
                    "raw_file_path": f"raw/public/{name}.json",
                    "scope_symbols": [str(r['symbol']) for r in packet['data'] if r.get('symbol')] if name == 'nasdaq_earnings' else ["*"],
                    "consumer_domains": ["event", "relationships", "price_volume"] if name == 'nasdaq_earnings' else ["market", "price_volume", "derivatives"],
                    "root_component_type": "event_calendar" if name == 'nasdaq_earnings' else "market_context"})
    collection_statuses: list[dict[str, str]] = [
        {"symbol": "*", "domain": "market", "status": "collected"},
        {"symbol": "*", "domain": "capital", "status": "no_data"},
    ]
    captured_at = now.isoformat()
    market_path = "raw/market/benchmarks.json"
    files[market_path] = _json_bytes({
        "daily_and_premarket": benchmark_states,
        "source": "yfinance_free_research",
    })
    records.append({
        "evidence_id": "ev_market_" + sha256_payload(benchmark_states)[:16],
        "domain": "market", "provider": profile.market_provider,
        "source_uri": "provider://market-and-sector-bars", "captured_at": captured_at,
        "raw_file_path": market_path, "scope_symbols": ["*"],
        "consumer_domains": ["market", "relationships", "price_volume"],
        "root_component_type": "market_context",
    })
    for candidate in candidates:
        symbol = candidate["symbol"]
        collection_statuses.extend([
            {"symbol": symbol, "domain": "relationships", "status": "collected"},
            {"symbol": symbol, "domain": "price_volume", "status": "collected"},
        ])
        price_path = f"raw/stocks/{symbol}.json"
        files[price_path] = _json_bytes({
            "symbol": symbol, **stock_states[symbol], "provider": profile.market_provider,
        })
        records.append({
            "evidence_id": f"ev_price_{symbol.lower()}_" + sha256_payload(stock_states[symbol])[:12],
            "domain": "price_volume", "provider": profile.market_provider,
            "source_uri": f"provider://unadjusted-bars/{symbol}",
            "captured_at": captured_at, "raw_file_path": price_path,
            "scope_symbols": [symbol],
            "consumer_domains": ["market", "relationships", "event", "derivatives", "price_volume"],
            "root_component_type": "stock_price_volume",
        })
        relationship = {
            "symbol": symbol, "company_name": candidate["company_name"],
            "gics_sector": candidate["gics_sector"],
            "gics_sub_industry": candidate["gics_sub_industry"],
            "sector_benchmark": candidate["sector_benchmark"],
            "instrument_metadata": metadata.get(symbol),
            "metadata_status": metadata_status,
            "economic_relationships_claimed": False,
        }
        relation_path = f"raw/relationships/{symbol}.json"
        files[relation_path] = _json_bytes(relationship)
        records.append({
            "evidence_id": f"ev_relationship_{symbol.lower()}_" + sha256_payload(relationship)[:12],
            "domain": "relationships", "provider": "pit-universe+financedatabase",
            "source_uri": f"provider://instrument-identity/{symbol}",
            "captured_at": captured_at, "raw_file_path": relation_path,
            "scope_symbols": [symbol],
            "consumer_domains": ["relationships", "price_volume"],
            "root_component_type": "industry_context",
        })
        event_rows = events.get(symbol, [])
        symbol_events = [row for row in event_rows if row.get("status") == "collected"]
        event_failed = any(row.get("status") == "provider_error" for row in event_rows)
        event_document_collected = False
        if symbol_events:
            candidate["captured_primary_event"] = True
        for event_index, event in enumerate(symbol_events):
            event_key = str(event.get("accession_number") or f"{symbol}-{event_index}")
            original_id = f"ev_sec_raw_{symbol.lower()}_{sha256_payload(event_key)[:12]}"
            source_url = str(event.get("source_url", ""))
            try:
                content = sec.download_primary_document(source_url)
                raw_document_path = f"raw/sec/{symbol}/{event_index}.html"
                files[raw_document_path] = content
                records.append({
                    "evidence_id": original_id, "domain": "event", "provider": "sec-edgar",
                    "source_uri": source_url, "captured_at": captured_at,
                    "published_at": event.get("acceptance_time"),
                    "upstream_event_id": event_key, "raw_file_path": raw_document_path,
                    "scope_symbols": [symbol], "consumer_domains": [],
                    "root_component_type": "stock_event",
                })
                event_view = {
                    **event,
                    "document_text": _document_text(
                        content, profile.maximum_event_characters
                    ),
                }
                event_path = f"raw/events/{symbol}/{event_index}.json"
                files[event_path] = _json_bytes(event_view)
                records.append({
                    "evidence_id": f"ev_event_{symbol.lower()}_{sha256_payload(event_key)[:12]}",
                    "domain": "event", "provider": "sec-edgar",
                    "source_uri": source_url, "captured_at": captured_at,
                    "published_at": event.get("acceptance_time"),
                    "upstream_event_id": event_key, "raw_file_path": event_path,
                    "parent_evidence_ids": [original_id], "scope_symbols": [symbol],
                    "consumer_domains": ["event", "relationships", "price_volume"],
                    "root_component_type": "stock_event",
                })
                event_document_collected = True
            except Exception:
                candidate["captured_primary_event"] = False
                event_failed = True
        collection_statuses.append({
            "symbol": symbol, "domain": "event",
            "status": (
                "collected" if event_document_collected
                else ("provider_error" if event_failed else "not_applicable")
            ),
        })
        try:
            option_surface = market.option_surface(symbol)
        except Exception as exc:
            option_surface = {"symbol": symbol, "status": "provider_error", "error_type": type(exc).__name__}
        option_path = f"raw/options/{symbol}.json"
        files[option_path] = _json_bytes(option_surface)
        if option_surface.get("status") == "collected":
            records.append({
                "evidence_id": f"ev_options_{symbol.lower()}_" + sha256_payload(option_surface)[:12],
                "domain": "derivatives", "provider": profile.market_provider,
                "source_uri": f"provider://option-surface/{symbol}",
                "captured_at": captured_at, "raw_file_path": option_path,
                "scope_symbols": [symbol], "consumer_domains": ["derivatives"],
                "root_component_type": "stock_derivatives",
            })
        collection_statuses.append({
            "symbol": symbol, "domain": "derivatives",
            "status": (
                "collected" if option_surface.get("status") == "collected"
                else ("provider_error" if option_surface.get("status") == "provider_error" else "no_data")
            ),
        })
    files["raw/provider-status.json"] = _json_bytes({
        "capital": {"status": "unavailable", "reason": "no aggressor-and-depth provider"},
        "option_trade_flow": {"status": "unavailable", "reason": "surface only"},
        "metadata": {"status": metadata_status},
    })
    completed = datetime.now(ET) if observed_at is None else now
    cutoff_status = "on_time" if completed <= scheduled_cutoff else "late_research_only"
    manifest = provider_manifest(
        profile=profile, universe_path=universe_path, cutoff=scheduled_cutoff
    )
    manifest["collection_completed_at_et"] = completed.isoformat()
    manifest["metadata_status"] = metadata_status
    manifest["collection_statuses"] = collection_statuses
    manifest["public_source_statuses"] = public_statuses
    return freeze_evidence_bundle(
        root=root, as_of_et=completed.isoformat(),
        scheduled_cutoff_et=scheduled_cutoff.isoformat(), cutoff_status=cutoff_status,
        candidates=candidates, records=records, files=files,
        provider_manifest=manifest,
    )
