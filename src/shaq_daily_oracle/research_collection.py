from __future__ import annotations

import csv
import json
import math
from functools import lru_cache
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .data_providers import (
    DataProfile,
    DataProviderError,
    FinanceDatabaseProvider,
    OpenBBProviderAdapter,
    SecEdgarProvider,
    YFinanceProvider,
    load_versioned_universe,
    provider_manifest,
    sec_exhibit_urls,
)
from .hashing import sha256_payload
from .data_retry import failure_diagnostic
from .market_calendar import market_session, previous_market_session, next_market_session
from .research_batch import FrozenEvidence, freeze_evidence_bundle
from .research_progress import safe_observe


class ResearchCollectionError(ValueError):
    """The cross-platform research evidence snapshot cannot be formed safely."""

    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


ET = ZoneInfo("America/New_York")


def history_lookback_days(package_root: Path) -> int:
    """Share the governed history window between warmup and evidence collection."""
    value = json.loads((package_root / 'config/price-history.json').read_text(encoding='utf-8'))['lookback_calendar_days']
    if type(value) is not int or value <= 0:
        raise ValueError('invalid history lookback window')
    return value


def today_collection_status(now: datetime) -> dict[str, Any]:
    """Use the scheduler's NYSE calendar and ET date, never a local weekday guess."""
    now = now.astimezone(ET)
    session = market_session(now.date())
    next_date = (session or next_market_session(now.date())).session_date.isoformat()
    if session is None:
        status, message = 'closed', f'今日美东休市；下次交易日 {next_date}，不启动今日研究。'
    elif now.time().replace(tzinfo=None) < time(4, 0):
        status, message = 'not_yet_premarket', f'尚未到美东 04:00 盘前时段；下次可采集交易日 {next_date}。'
    else:
        status, message = 'ready', '运行时核验当天盘前数据；缺失时不调用模型。'
    return {'et': now.isoformat(), 'trade_date': now.date().isoformat(),
            'is_trading_day': session is not None, 'next_trade_date': next_date,
            'today_available': status == 'ready', 'today_status': status, 'today_message': message}


def validate_today_evidence(evidence: FrozenEvidence, now: datetime) -> None:
    """Reject stale/legacy locators without altering their immutable evidence."""
    now = now.astimezone(ET)
    cutoff = _timestamp(evidence.manifest.get('scheduled_cutoff_et'))
    observed = _timestamp(evidence.manifest.get('as_of_et'))
    expected = datetime.combine(now.date(), time(8, 50), ET)
    if cutoff != expected or observed is None or observed.date() != now.date() or observed > now:
        raise ResearchCollectionError('cached_evidence_invalid：缓存不是当天有效截止时间的证据；未启动今日研究。')
    observations = evidence.manifest.get('provider_manifest', {}).get('premarket_observations', {})
    valid = False
    for row in observations.values():
        first, last = _timestamp(row.get('first_observation_et')), _timestamp(row.get('last_observation_et'))
        if row.get('status') == 'collected' and first and last and (
            datetime.combine(now.date(), time(4, 0), ET) <= first <= last <= min(now, cutoff)
        ):
            valid = True
    if not valid:
        raise ResearchCollectionError('no_data：未取得当天盘前数据；缓存不能用于今日研究。')


def _document_text(content: bytes, maximum_characters: int) -> str:
    from .filing_documents import document_policy, parse_document, select_documents
    policy = document_policy()
    document = parse_document(content, source_uri='', policy=policy)
    return select_documents([document], maximum_characters=maximum_characters, policy=policy)[0]['document_text']


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


@lru_cache()
def _prior_daily_sessions(session_date: date) -> tuple[date, date]:
    prior = previous_market_session(session_date).session_date
    return prior, previous_market_session(prior).session_date


def _daily_state(records: list[dict[str, Any]], session_date: date) -> dict[str, Any]:
    eligible = []
    for row in _sorted(records):
        observed = _timestamp(row.get("timestamp"))
        close = _number(row.get("close"))
        if observed is not None and observed.date() < session_date and close is not None and close > 0:
            eligible.append((observed, row, close))
    by_date = {item[0].date(): item[2] for item in eligible}
    prior_session, before_prior = _prior_daily_sessions(session_date)
    prior_close = by_date.get(prior_session)
    before_close = by_date.get(before_prior)
    return {
        "previous_close": prior_close,
        "previous_return": (
            prior_close / before_close - 1
            if prior_close is not None and before_close is not None else None
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
            and observed <= cutoff and close is not None and close > 0
        ):
            eligible.append((observed, row, close))
    last_price = eligible[-1][2] if eligible else None
    volumes = [_number(item[1].get("volume")) for item in eligible]
    present_volumes = [value for value in volumes if value is not None and value >= 0]
    positive_volume_count = sum(value > 0 for value in present_volumes)
    zero_volume_count = sum(value == 0 for value in present_volumes)
    missing_volume_count = len(volumes) - len(present_volumes)
    if not eligible:
        volume_status = "no_price_bars"
        volume_note = "no eligible premarket price bars were collected"
    elif missing_volume_count == len(eligible):
        volume_status = "missing"
        volume_note = "provider did not supply volume fields for eligible price bars"
    elif missing_volume_count:
        volume_status = "partially_missing"
        volume_note = "provider supplied volume for only part of the eligible price bars"
    elif positive_volume_count:
        volume_status = "observed_positive"
        volume_note = "provider reported positive volume in at least one eligible price bar"
    else:
        volume_status = "volume_unavailable"
        volume_note = "all provider volume fields were zero; this does not prove no trading occurred"
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
        "observed_volume": sum(present_volumes) if positive_volume_count else None,
        "volume_status": volume_status,
        "volume_note": volume_note,
        "eligible_price_bar_count": len(eligible),
        "volume_observation_count": len(present_volumes),
        "positive_volume_bar_count": positive_volume_count,
        "zero_volume_bar_count": zero_volume_count,
        "missing_volume_bar_count": missing_volume_count,
        "volume_ranking_eligible": volume_status == "observed_positive",
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
        ranking_volume = (
            float(premarket.get("observed_volume") or 0)
            if premarket.get("volume_ranking_eligible") is True else 0.0
        )
        ranked.append((
            -metric, -ranking_volume, member.symbol,
            {
                "symbol": member.symbol,
                "company_name": member.company_name,
                "gics_sector": member.gics_sector,
                "gics_sub_industry": member.gics_sub_industry,
                "sector_benchmark": benchmark,
                "selection_method": method,
                "selection_metric": metric,
                "premarket_return": stock_gap,
                "premarket_volume": premarket.get("observed_volume"),
                "premarket_volume_status": premarket.get("volume_status"),
                "premarket_volume_note": premarket.get("volume_note"),
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
    alpaca_credentials: tuple[str, str] | None = None,
    screening_rules: dict[str, str] | None = None,
    history_cache_root: Path | None = None,
    allow_replay: bool = False,
    observer: Callable[..., None] | None = None,
    collection_clock: Callable[[], datetime] | None = None,
) -> FrozenEvidence:
    collection_clock = collection_clock or (lambda: datetime.now(ET))
    def observe(component: str, status: str, *, source: str, completed: int,
                total: int, message: str, **details: Any) -> None:
        safe_observe(
            observer, stage="data_preparation", component=component,
            status=status, source=source, completed=completed, total=total,
            message=message, **details,
        )

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
    observe("universe", "running", source=str(universe_path), completed=0, total=1,
            message="读取候选池与市场基准")
    try:
        members = load_versioned_universe(universe_path, cutoff=data_cutoff)
        benchmark_path = package_root / "config/market-benchmarks.csv"
        benchmark_symbols, sector_etf = _benchmark_rows(benchmark_path)
    except Exception:
        observe("universe", "failed", source=str(universe_path), completed=0, total=1,
                message="候选池或市场基准读取失败")
        raise
    observe("universe", "complete", source=str(universe_path), completed=1, total=1,
            message="候选池与市场基准读取完成", symbol_count=len(members))
    openbb = OpenBBProviderAdapter(
        profile=profile, api_key=openbb_api_key,
        sec_user_agent=sec_identity,
    ) if "openbb-rest" in {
        profile.market_provider, profile.metadata_provider, profile.event_provider
    } else None
    market = market_provider or (
        openbb if profile.market_provider == "openbb-rest" else YFinanceProvider(profile)
    )
    if isinstance(market, YFinanceProvider) and observer is not None:
        # Retry telemetry is display-only and never enters frozen evidence.
        market.progress_observer = lambda **event: safe_observe(observer, **event)
    if isinstance(market, YFinanceProvider) and history_cache_root is not None:
        # One session only: a new trading day independently re-observes historical prices.
        market.history_checkpoint_root = (history_cache_root.parent / 'collection_requests'
                                          / session.session_date.isoformat() / profile.history_identity())
    if market_provider is None and isinstance(market, YFinanceProvider):
        from .public_history_recovery import wrap_public_recovery
        market = wrap_public_recovery(market, package_root,
            checkpoint_root=market.history_checkpoint_root, etfs=benchmark_symbols)
    public_config_path = package_root / "config/public-data.json"
    public_config = json.loads(public_config_path.read_text(encoding="utf-8")) if public_config_path.exists() else None
    if history_cache_root is not None and public_config:
        from .public_data import DailyBarCache
        market = DailyBarCache(market, history_cache_root / profile.history_identity(), overlap_days=public_config["history_overlap_days"])
    lookback_start = session.session_date - timedelta(days=history_lookback_days(package_root))
    stock_symbols = [member.symbol for member in members]

    def grouped_market_requests(component: str) -> tuple[
        dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]
    ]:
        requests = [
            (scope, symbols[start:start + profile.batch_size])
            for scope, symbols in (("stocks", stock_symbols), ("benchmarks", benchmark_symbols))
            for start in range(0, len(symbols), profile.batch_size)
        ]
        total = len(requests)
        completed = 0
        returned_symbol_count = 0
        outputs: dict[str, dict[str, list[dict[str, Any]]]] = {
            "stocks": {}, "benchmarks": {},
        }
        observe(component, "running", source=profile.market_provider,
                completed=0, total=total, message="开始分组获取市场数据",
                returned_symbol_count=0)
        for scope, group in requests:
            try:
                if component == "history":
                    rows = market.history(
                        group, start=lookback_start, end=session.session_date,
                        interval="1d",
                    )
                else:
                    rows = market.recent_intraday(group, cutoff=data_cutoff)
            except Exception:
                observe(component, "failed", source=profile.market_provider,
                        completed=completed, total=total,
                        message="市场数据分组请求失败",
                        returned_symbol_count=returned_symbol_count)
                raise
            outputs[scope].update(rows)
            completed += 1
            returned_symbol_count += sum(bool(rows.get(symbol)) for symbol in group)
            observe(component, "running", source=profile.market_provider,
                    completed=completed, total=total,
                    message="市场数据分组请求已完成",
                    returned_symbol_count=returned_symbol_count)
        if component == "history":
            observe(component, "complete", source=profile.market_provider,
                    completed=completed, total=total,
                    message="历史行情请求已检查；返回数据与请求完成分开计数",
                    returned_symbol_count=returned_symbol_count)
        return outputs["stocks"], outputs["benchmarks"]

    try:
        stock_daily, benchmark_daily = grouped_market_requests("history")
        stock_intraday, benchmark_intraday = grouped_market_requests("premarket")
    except DataProviderError as exc:
        from .data_retry import failure_message
        reason = failure_message(exc.diagnostic)
        raise ResearchCollectionError(f'provider_error：{reason}，未启动模型分析。',
                                      diagnostic=exc.diagnostic) from exc
    premarket_observations = {symbol: {
        key: state[key] for key in (
            'status', 'first_observation_et', 'last_observation_et',
            'volume_status', 'eligible_price_bar_count', 'volume_observation_count',
            'positive_volume_bar_count', 'zero_volume_bar_count',
            'missing_volume_bar_count',
        )
    } for symbol in stock_symbols for state in [_premarket_state(
        stock_intraday.get(symbol, []), session_date=session.session_date,
        cutoff=data_cutoff, previous_close=None)]}
    eligible_symbol_count = sum(
        row["status"] == "collected" for row in premarket_observations.values()
    )
    premarket_total = sum(
        (len(symbols) + profile.batch_size - 1) // profile.batch_size
        for symbols in (stock_symbols, benchmark_symbols)
    )
    observe("premarket", "complete" if eligible_symbol_count else "unavailable",
            source=profile.market_provider, completed=premarket_total,
            total=premarket_total,
            message="盘前请求已检查；有效价格资料单独计数",
            returned_symbol_count=sum(bool(rows) for rows in stock_intraday.values())
            + sum(bool(rows) for rows in benchmark_intraday.values()),
            eligible_symbol_count=eligible_symbol_count)
    if not allow_replay and not any(row['status'] == 'collected' for row in premarket_observations.values()):
        raise ResearchCollectionError('no_data：未取得当天盘前数据；不使用上一交易日分钟数据启动今日研究。')
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
    alpaca_volume, alpaca_volume_error = None, None
    if profile.alpaca_premarket_enabled:
        from .alpaca_bars import collect_premarket_volume, AlpacaBarsError
        observe('premarket', 'running', source='alpaca-sip', completed=0, total=1,
                message='补充候选股票的延迟全市场盘前成交量')
        try:
            key, secret = alpaca_credentials or ('', '')
            alpaca_volume = collect_premarket_volume(symbols=candidate_symbols,
                session_date=session.session_date, cutoff=scheduled_cutoff,
                observed_at=collection_clock(),
                key_id=key, secret_key=secret, base_url=profile.alpaca_data_base_url, feed='sip',
                delay_seconds=profile.alpaca_sip_delay_seconds,
                safety_margin_seconds=profile.alpaca_safety_margin_seconds,
                delay_source='https://docs.alpaca.markets/us/docs/market-data-faq',
                timeout_seconds=profile.request_timeout_seconds,
                clock=collection_clock)
        except AlpacaBarsError as exc:
            alpaca_volume_error = {'kind': exc.kind}
        observe('premarket', 'complete' if alpaca_volume else 'failed', source='alpaca-sip',
            completed=1 if alpaca_volume else 0, total=1,
            message='候选盘前成交量补充完成' if alpaca_volume else 'Alpaca 成交量未取得；原始价格资料保留',
            diagnostic=alpaca_volume_error)
    member_by_symbol = {member.symbol: member for member in members}
    iex_quotes, iex_failure = None, None
    if profile.alpaca_orderflow_enabled:
        from .alpaca_iex_quotes import collect_quote_pressure
        observe('capital', 'running', source='alpaca-quotes', completed=0, total=1,
                message='读取买卖报价；IEX 无合格资料时补取延迟全市场报价')
        try:
            key, secret = alpaca_credentials or ('', '')
            iex_quotes = collect_quote_pressure(key_id=key, secret=secret,
                cutoff=scheduled_cutoff.isoformat(), observed_at=collection_clock().isoformat(),
                symbols=candidate_symbols, window_seconds=profile.alpaca_quote_window_seconds,
                max_pages_per_symbol=profile.alpaca_quote_max_pages,
                timeout_seconds=profile.request_timeout_seconds,
                max_response_bytes=profile.alpaca_quote_max_bytes, max_attempts=profile.alpaca_quote_attempts,
                delay_seconds=profile.alpaca_sip_delay_seconds,
                safety_margin_seconds=profile.alpaca_safety_margin_seconds,
                session_fallback=profile.alpaca_quote_session_fallback,
                retry_backoff_seconds=profile.alpaca_quote_retry_backoff_seconds,
                retry_wait_cap_seconds=profile.alpaca_quote_retry_wait_cap_seconds,
                clock=collection_clock)
        except Exception as exc:
            iex_failure = failure_diagnostic(exc, None)
    metadata: dict[str, Any] = {}
    metadata_status = "not_configured"
    metadata_source = {"provider": profile.metadata_provider}
    metadata_diagnostic = {}
    if profile.metadata_provider != "none":
        observe("metadata", "running", source=profile.metadata_provider,
                completed=0, total=1, message="开始获取候选标的资料")
    if profile.metadata_provider == "financedatabase":
        try:
            identity_provider = metadata_provider or FinanceDatabaseProvider(
                timeout_seconds=profile.request_timeout_seconds,
                cache_root=(history_cache_root.parent / 'instrument_metadata'
                            if history_cache_root is not None else None),
            )
            metadata = identity_provider.metadata(candidate_symbols)
            metadata_source.update(getattr(identity_provider, 'receipt', {}))
            metadata_status = "collected" if metadata else "no_data"
        except Exception as exc:
            metadata_status = f"provider_error:{type(exc).__name__}"
            metadata_diagnostic = failure_diagnostic(exc, None)
            observe('metadata', 'retrying', source='sec-edgar', completed=0, total=1,
                    message='股票资料库暂不可用，正在从 SEC 核对公司名称与交易所')
            try:
                fallback = SecEdgarProvider(user_agent=sec_identity,
                    timeout_seconds=profile.request_timeout_seconds,
                    cache_root=(history_cache_root.parent / 'sec_instrument_metadata'
                                if history_cache_root is not None else None))
                metadata = fallback.instrument_metadata(
                    [member_by_symbol[symbol] for symbol in candidate_symbols])
                metadata_source = {'provider': 'sec-edgar',
                                   **getattr(fallback, 'metadata_receipt', {}),
                                   'primary_failure': metadata_diagnostic}
                metadata_status = 'collected' if metadata else 'no_data'
            except Exception as fallback_exc:
                metadata_diagnostic = failure_diagnostic(fallback_exc, None)
                metadata_source = {'provider': 'sec-edgar', 'primary_provider': profile.metadata_provider}
    elif profile.metadata_provider == "openbb-rest":
        try:
            metadata = (metadata_provider or openbb).metadata(candidate_symbols)
            metadata_status = "collected"
        except Exception as exc:
            metadata_status = f"provider_error:{type(exc).__name__}"
    observe(
        "metadata",
        "failed" if metadata_status.startswith("provider_error") else
        ("unavailable" if metadata_status in {"not_configured", "no_data"} else "complete"),
        source=metadata_source['provider'],
        completed=1 if metadata_status == "collected" else 0,
        total=0 if metadata_status == "not_configured" else 1,
        message=(f"已取得 {len(metadata)} / {len(candidate_symbols)} 只股票资料"
                 + ('（SEC 公司身份；行业沿用股票池资料）' if metadata_source['provider'] == 'sec-edgar' else '')
                 + ('；使用已核验缓存，更新时间见来源' if metadata_source.get('cache_status') == 'cached_after_refresh_failure' else '')
                 if metadata_status == 'collected' else
                 ('股票资料下载超时，未补造资料' if metadata_diagnostic.get('kind') == 'timeout'
                  else '股票资料未取得，请检查数据来源或网络')),
        returned_symbol_count=len(metadata),
        diagnostic=metadata_diagnostic, metadata_source=metadata_source,
    )
    previous = previous_market_session(session.session_date)
    events: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in candidate_symbols}
    sec = event_provider or (
        openbb if profile.event_provider == "openbb-rest" else SecEdgarProvider(
            user_agent=sec_identity, timeout_seconds=profile.request_timeout_seconds
        )
    )
    observe("events", "running", source=profile.event_provider,
            completed=0, total=len(candidate_symbols), message="开始核对候选公司事件")
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
    # Recovery receipts are audit artifacts, not additional votes or model tasks.
    for digest, source in getattr(market, 'source_documents', {}).items():
        files[f'raw/provider_sources/{digest}.json'] = _json_bytes(source)
    files['raw/market_collection_status.json'] = _json_bytes({
        'observations': getattr(market, 'diagnostics', []),
        'request_profile': profile.source_dict(),
    })
    if screening_rules:
        files["raw/screening.json"] = _json_bytes({"pool": full_pool, "candidate_sets": candidate_sets})
    records: list[dict[str, Any]] = []
    public_statuses = []
    if public_config and market_provider is None:
        from .public_data import collect_public_context
        public_total = len(public_config["sources"])
        observe("public_context", "running", source="configured_public_sources",
                completed=0, total=public_total, message="开始获取公开市场背景")
        try:
            public_packets = collect_public_context(public_config, cutoff=scheduled_cutoff)
        except Exception:
            observe("public_context", "failed", source="configured_public_sources",
                    completed=0, total=public_total, message="公开市场背景获取失败")
            raise
        public_completed = 0
        public_with_data = 0
        public_failed = False
        for packet in public_packets:
            name = packet["provider"]
            packet_status = packet["status"]
            public_completed += packet_status == "collected"
            public_with_data += packet_status == "collected" and bool(packet.get("data"))
            public_failed |= packet_status == "provider_error"
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
            observe("public_context", "running", source="configured_public_sources",
                    completed=public_completed, total=public_total,
                    message="公开市场背景来源已检查",
                    checked=len(public_statuses), returned_source_count=public_with_data)
        observe("public_context", "failed" if public_failed else
                ("complete" if public_completed else "unavailable"),
                source="configured_public_sources", completed=public_completed,
                total=public_total, message="公开市场背景来源状态已记录",
                checked=len(public_statuses), returned_source_count=public_with_data)
    else:
        observe("public_context", "unavailable", source="configured_public_sources",
                completed=0, total=0, message="当前采集路径未启用公开市场背景")
    collection_statuses: list[dict[str, Any]] = [
        {"symbol": "*", "domain": "market", "status": "collected"},
        {"symbol": "*", "domain": "capital", "status": "no_data"},
    ]
    captured_at = now.isoformat()
    iex_eligible = 0
    if iex_quotes:
        for symbol, quote in iex_quotes['symbols'].items():
            feed = quote.get('feed', iex_quotes['feed'])
            provider = 'alpaca-' + feed
            captured = quote.get('capture_completed_at', iex_quotes['capture_completed_at'])
            for index, page in enumerate(quote.get('fallback_raw_pages', [])):
                files[f'raw/alpaca/{symbol}-quote-attempt-{index}.json'] = page['body_utf8'].encode('utf-8')
            parent_ids = []
            for index, page in enumerate(quote['raw_pages']):
                path = f'raw/alpaca/{symbol}-quote-page-{index}.json'
                files[path] = page['body_utf8'].encode('utf-8')
                eid = f'ev_alpaca_{feed}_raw_{symbol.lower()}_' + sha256_payload([page['body_utf8'], index])[:12]
                parent_ids.append(eid)
                records.append({'evidence_id': eid, 'domain': 'capital', 'provider': provider,
                    'source_uri': page['request_uri'], 'captured_at': captured,
                    'raw_file_path': path, 'scope_symbols': [symbol], 'consumer_domains': [],
                    'root_component_type': 'capital_flow'})
            eligible = quote['formal_cutoff_eligible'] and bool(parent_ids)
            # Raw late/failed captures remain diagnostic files, never task evidence.
            if not eligible:
                records[:] = [r for r in records if r['evidence_id'] not in parent_ids]
            view = {**{k: v for k, v in iex_quotes.items() if k != 'symbols'},
                    **{k: v for k, v in quote.items() if k not in {'raw_pages', 'fallback_raw_pages'}}, 'symbol': symbol,
                    'interpretation_boundary': ('Delayed SIP best-quote pressure; quoted venues may change. '
                        if feed == 'sip' else 'One-venue displayed quote pressure. ') +
                        'Only the actual observed window; not contemporaneous with later prices. '
                        'If recent_pressure_available is false, use timestamped quotes and session_context '
                        'as historical liquidity context only, not evidence of current buying/selling direction. '
                        'Not full-market capital flow, native trade aggressor, or an all-day directional forecast.'}
            path = f'raw/alpaca/{symbol}-quote-context.json'
            files[path] = _json_bytes(view)
            if eligible:
                iex_eligible += 1
                records.append({'evidence_id': f'ev_alpaca_{feed}_' + sha256_payload(view)[:16],
                    'domain': 'capital', 'provider': provider, 'source_uri': iex_quotes['source_uri'],
                    'captured_at': captured, 'raw_file_path': path,
                    'parent_evidence_ids': parent_ids, 'scope_symbols': [symbol],
                    'consumer_domains': ['capital', 'price_volume'], 'root_component_type': 'capital_flow'})
            collection_statuses.append({'symbol': symbol, 'domain': 'capital',
                'status': 'collected' if eligible else ('provider_error' if quote['status']=='provider_error' else 'no_data'),
                'evidence_kind': quote['status'],
                'reason': None if eligible else quote.get('error_type') or quote['calculation'].get('reason')})
    if profile.alpaca_orderflow_enabled:
        observe('capital', 'complete' if iex_eligible else 'unavailable', source='alpaca-quotes',
            completed=iex_eligible, total=len(candidate_symbols),
            message=f'买卖报价已取得 {iex_eligible} / {len(candidate_symbols)} 只', diagnostic=iex_failure)
    if alpaca_volume:
        volume_meta = alpaca_volume['metadata']
        raw_ids = []
        for index, payload in enumerate(alpaca_volume.pop('raw_pages')):
            path = f'raw/alpaca/minute-page-{index}.json'
            files[path] = payload
            eid = 'ev_alpaca_minute_raw_' + sha256_payload([volume_meta['response_sha256'][index], index])[:16]
            raw_ids.append(eid)
            records.append({'evidence_id': eid, 'domain': 'price_volume', 'provider': 'alpaca-sip',
                'source_uri': volume_meta['source_uri'], 'captured_at': volume_meta['captured_at_et'],
                'raw_file_path': path, 'scope_symbols': candidate_symbols, 'consumer_domains': [],
                'root_component_type': 'stock_price_volume'})
        for symbol, values in alpaca_volume['symbols'].items():
            if values['volume_status'] != 'observed_positive':
                continue
            view = {'symbol': symbol, **values, 'source': volume_meta,
                    'interpretation_boundary': 'Delayed consolidated volume; not contemporaneous with later Yahoo prices. '
                        'Not aggressor flow, not buy/sell direction, not an extension of the quote window.'}
            path = f'raw/alpaca/{symbol}-premarket-volume.json'
            files[path] = _json_bytes(view)
            records.append({'evidence_id': 'ev_alpaca_volume_' + sha256_payload(view)[:16],
                'domain': 'price_volume', 'provider': 'alpaca-sip', 'source_uri': volume_meta['source_uri'],
                'captured_at': volume_meta['captured_at_et'], 'raw_file_path': path,
                'parent_evidence_ids': raw_ids, 'scope_symbols': [symbol],
                'consumer_domains': ['price_volume', 'event', 'relationships'],
                'root_component_type': 'stock_price_volume'})
        files['raw/alpaca/premarket-status.json'] = _json_bytes(alpaca_volume)
    elif profile.alpaca_premarket_enabled:
        files['raw/alpaca/premarket-status.json'] = _json_bytes({'status': 'provider_error', 'diagnostic': alpaca_volume_error})
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
    events_checked = 0
    events_completed = 0
    event_document_count = 0
    option_checked = 0
    option_context_count = 0
    option_sources = profile.market_provider + (' + OCC' if profile.occ_open_interest_enabled else '')
    from .filing_documents import document_policy, parse_document, select_documents
    filing_policy = document_policy(package_root)
    option_completed = 0
    option_chain_count = 0
    option_failed = False
    observe("options", "running", source=profile.market_provider,
            completed=0, total=len(candidates), message="开始逐只核对期权链",
            checked=0, chain_count=0)
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
            "metadata_source": metadata_source,
            "economic_relationships_claimed": False,
        }
        relation_path = f"raw/relationships/{symbol}.json"
        files[relation_path] = _json_bytes(relationship)
        records.append({
            "evidence_id": f"ev_relationship_{symbol.lower()}_" + sha256_payload(relationship)[:12],
            "domain": "relationships", "provider": "pit-universe+" + metadata_source['provider'],
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
                event_view = {**event}
                documents = [parse_document(content, source_uri=source_url, policy=filing_policy)]
                document_archives = [raw_document_path.replace('.html', '-blocks.json')]
                parent_ids = [original_id]
                exhibit_urls = sec_exhibit_urls(source_url, content)
                exhibit_rows, exhibit_errors = [], []
                for attachment_index, exhibit_url in enumerate(exhibit_urls[:profile.maximum_event_exhibits]):
                    try:
                        exhibit_content = sec.download_primary_document(exhibit_url)
                        exhibit_path = f"raw/sec/{symbol}/{event_index}-exhibit-{attachment_index}.html"
                        exhibit_id = f"ev_sec_exhibit_{symbol.lower()}_" + sha256_payload(exhibit_url)[:12]
                        files[exhibit_path] = exhibit_content
                        records.append({
                            "evidence_id": exhibit_id, "domain": "event", "provider": "sec-edgar",
                            "source_uri": exhibit_url, "captured_at": captured_at,
                            "published_at": event.get("acceptance_time"),
                            "upstream_event_id": event_key, "raw_file_path": exhibit_path,
                            "parent_evidence_ids": [original_id],
                            "scope_symbols": [symbol], "consumer_domains": [],
                            "root_component_type": "stock_event",
                        })
                        parent_ids.append(exhibit_id)
                        documents.append(parse_document(exhibit_content, source_uri=exhibit_url, policy=filing_policy))
                        document_archives.append(exhibit_path.replace('.html', '-blocks.json'))
                        exhibit_rows.append({"source_url": exhibit_url, "raw_evidence_id": exhibit_id})
                        event_document_count += 1
                    except Exception as exc:
                        exhibit_errors.append({"source_url": exhibit_url,
                            "diagnostic": failure_diagnostic(exc, None)})
                views = select_documents(documents, maximum_characters=profile.maximum_event_characters,
                                         policy=filing_policy)
                for document, archive, view in zip(documents, document_archives, views, strict=True):
                    files[archive] = _json_bytes(document)
                    view['document_coverage']['archive_path'] = archive
                event_view.update(views[0])
                for exhibit, view in zip(exhibit_rows, views[1:], strict=True):
                    exhibit.update(view)
                event_view['exhibits'] = exhibit_rows
                event_view['exhibit_collection'] = {"discovered": len(exhibit_urls),
                    "collected": len(exhibit_rows), "failures": exhibit_errors,
                    "not_fetched_due_to_limit": max(0, len(exhibit_urls) - profile.maximum_event_exhibits)}
                event_failed = event_failed or bool(exhibit_errors)
                event_path = f"raw/events/{symbol}/{event_index}.json"
                files[event_path] = _json_bytes(event_view)
                records.append({
                    "evidence_id": f"ev_event_{symbol.lower()}_{sha256_payload(event_key)[:12]}",
                    "domain": "event", "provider": "sec-edgar",
                    "source_uri": source_url, "captured_at": captured_at,
                    "published_at": event.get("acceptance_time"),
                    "upstream_event_id": event_key, "raw_file_path": event_path,
                    "parent_evidence_ids": parent_ids, "scope_symbols": [symbol],
                    "consumer_domains": ["event", "relationships", "price_volume"],
                    "root_component_type": "stock_event",
                })
                event_document_collected = True
                event_document_count += 1
            except Exception:
                event_failed = True
        candidate["captured_primary_event"] = event_document_collected
        collection_statuses.append({
            "symbol": symbol, "domain": "event",
            "partial_failure": event_failed and event_document_collected,
            "reason": 'exhibit_or_primary_download_failed' if event_failed else None,
            "status": (
                "collected" if event_document_collected
                else ("provider_error" if event_failed else "not_applicable")
            ),
        })
        events_checked += 1
        if not event_failed:
            events_completed += 1
        observe("events", "running", source=profile.event_provider,
                completed=events_completed, total=len(candidates),
                message="候选公司事件已核对", checked=events_checked,
                document_count=event_document_count)
        try:
            option_surface = market.option_surface(symbol)
        except Exception as exc:
            option_surface = {"symbol": symbol, "status": "provider_error", "error_type": type(exc).__name__}
        option_surface = dict(option_surface)
        quality = option_surface.get("quality") or {}
        for target, source in (
            ("source_contract_count", "source_contract_count"),
            ("retained_contract_count", "retained_contract_count"),
            ("valid_two_sided_price_count", "contracts_with_valid_two_sided_price"),
        ):
            option_surface.setdefault(target, quality.get(source))
        option_task_eligible = (
            option_surface.get("status") == "collected"
            and option_surface.get("quote_timestamp_status") == "available"
            and option_surface.get("quote_freshness_eligible") is True
        )
        option_surface["derivatives_task_eligible"] = option_task_eligible
        if option_surface.get("status") == "collected" and not option_task_eligible:
            option_surface["derivatives_task_ineligibility_reason"] = (
                "quote_not_fresh" if option_surface.get("quote_timestamp_status") == "available"
                else "missing_exchange_quote_timestamp"
            )
        option_path = f"raw/options/{symbol}.json"
        files[option_path] = _json_bytes(option_surface)
        interest_context = None
        interest_failure = None
        if profile.occ_open_interest_enabled:
            from .occ_options import fetch_occ_series_search, parse_occ_series_search
            try:
                interest_raw = fetch_occ_series_search(symbol, timeout_seconds=profile.request_timeout_seconds)
                interest_captured = collection_clock()
                if interest_captured > scheduled_cutoff:
                    raise DataProviderError('OCC response arrived after evidence cutoff')
                interest = parse_occ_series_search(interest_raw, symbol=symbol, captured_at=interest_captured)
                contracts = [row for row in interest['contracts'] if row['expiration'] >= session.session_date.isoformat()]
                by_expiry = {}
                for contract in contracts:
                    expiry = by_expiry.setdefault(contract['expiration'], {'call_open_interest': 0, 'put_open_interest': 0,
                                                                          'contract_count': 0})
                    expiry[contract['option_type'] + '_open_interest'] += contract['open_interest']
                    expiry['contract_count'] += 1
                if not contracts:
                    raise DataProviderError('OCC has no unexpired standard contracts')
                interest_context = {key: value for key, value in interest.items() if key != 'contracts'}
                interest_context.update({'by_expiration': by_expiry, 'contract_count': len(contracts),
                    'total_call_open_interest': sum(r['open_interest'] for r in contracts if r['option_type']=='call'),
                    'total_put_open_interest': sum(r['open_interest'] for r in contracts if r['option_type']=='put'),
                    'quote_surface_eligible': False, 'directional_flow_eligible': False,
                    'interpretation_boundary': 'Open interest counts outstanding positions, not buyer direction. '
                        'No bid/ask, IV, aggressor, opening/closing trades or directional signal is supplied.'})
                raw_path = f'raw/options/{symbol}-occ.txt'
                context_path = f'raw/options/{symbol}-occ-context.json'
                files[raw_path] = interest_raw
                files[context_path] = _json_bytes(interest_context)
                root_id = f'ev_occ_raw_{symbol.lower()}_' + interest['source_sha256'][:12]
                common = {'domain': 'derivatives', 'provider': 'occ', 'source_uri': interest['source_uri'],
                    'captured_at': interest_captured.isoformat(), 'scope_symbols': [symbol], 'root_component_type': 'stock_derivatives'}
                records.extend([
                    {**common, 'evidence_id': root_id, 'raw_file_path': raw_path, 'consumer_domains': []},
                    {**common, 'evidence_id': f'ev_occ_{symbol.lower()}_' + sha256_payload(interest_context)[:12],
                     'raw_file_path': context_path, 'parent_evidence_ids': [root_id], 'consumer_domains': ['derivatives']},
                ])
            except Exception as exc:
                interest_failure = failure_diagnostic(exc, None)
                files[f'raw/options/{symbol}-occ-status.json'] = _json_bytes({'status':'provider_error',
                    'diagnostic': interest_failure})
        if option_task_eligible:
            records.append({
                "evidence_id": f"ev_options_{symbol.lower()}_" + sha256_payload(option_surface)[:12],
                "domain": "derivatives", "provider": profile.market_provider,
                "source_uri": f"provider://option-surface/{symbol}",
                "captured_at": captured_at, "raw_file_path": option_path,
                "scope_symbols": [symbol], "consumer_domains": ["derivatives"],
                "root_component_type": "stock_derivatives",
            })
        option_status = (
            "collected" if option_task_eligible or interest_context
            else ("provider_error" if option_surface.get("status") == "provider_error" else "no_data")
        )
        if interest_context and not option_task_eligible:
            option_reason = 'open_interest_context_only'
        elif option_status == "provider_error":
            option_reason = "provider_error"
        elif option_surface.get("status") == "no_data":
            option_reason = "no_option_chain"
        elif not option_task_eligible:
            option_reason = option_surface.get("derivatives_task_ineligibility_reason")
        else:
            option_reason = None
        collection_statuses.append({
            "symbol": symbol, "domain": "derivatives",
            "status": option_status,
            "reason": option_reason,
            "error_type": option_surface.get("error_type"),
            "source_contract_count": option_surface["source_contract_count"],
            "retained_contract_count": option_surface["retained_contract_count"],
            "valid_two_sided_price_count": option_surface["valid_two_sided_price_count"],
            "quote_timestamp_status": option_surface.get("quote_timestamp_status"),
            "open_interest_context_available": bool(interest_context),
            "open_interest_context_contracts": interest_context['contract_count'] if interest_context else None,
            "open_interest_context_diagnostic": interest_failure,
        })
        option_checked += 1
        option_context_count += bool(interest_context)
        if option_status == "provider_error":
            option_failed = True
        else:
            option_completed += 1
            option_chain_count += option_surface.get("status") == "collected"
        observe("options", "running", source=option_sources,
                completed=option_completed, total=len(candidates),
                message="候选期权链已核对", checked=option_checked,
                chain_count=option_chain_count, open_interest_context_count=option_context_count)
    observe("events", "complete" if events_completed == len(candidates) else "failed",
            source=profile.event_provider, completed=events_completed,
            total=len(candidates), message="公司事件核对结果已记录",
            checked=events_checked, document_count=event_document_count)
    observe("options", "failed" if option_failed else
            ("complete" if option_chain_count or option_context_count else "unavailable"),
            source=option_sources, completed=option_completed,
            total=len(candidates), message="期权报价与持仓结构核对结果已记录",
            checked=option_checked, chain_count=option_chain_count, open_interest_context_count=option_context_count)
    files["raw/provider-status.json"] = _json_bytes({
        "capital": {
            "status": "collected" if iex_eligible else "unavailable",
            "reason": "observed_quote_liquidity" if iex_eligible else (
                "no_eligible_order_book_source" if profile.alpaca_orderflow_enabled else "requires_authorized_aggressor_and_depth_feed"),
            "eligible_symbols": iex_eligible, "diagnostic": iex_failure,
            "detail": "IEX or delayed SIP quotes: recent pressure separated from earlier session liquidity; never native aggressor or full-market capital flow",
        },
        "option_trade_flow": {
            "status": "unavailable",
            "reason": "surface_without_aggressor_or_open_close_semantics",
            "detail": "chain coverage and quote quality do not identify aggressor side or opening versus closing trades",
        },
        "metadata": {"status": metadata_status, "source": metadata_source,
                     "diagnostic": metadata_diagnostic},
    })
    completed = datetime.now(ET) if observed_at is None else now
    cutoff_status = "on_time" if completed <= scheduled_cutoff else "late_research_only"
    manifest = provider_manifest(
        profile=profile, universe_path=universe_path, cutoff=scheduled_cutoff
    )
    manifest["collection_completed_at_et"] = completed.isoformat()
    manifest["metadata_status"] = metadata_status
    manifest["collection_statuses"] = collection_statuses
    manifest['filing_input_policy'] = filing_policy
    manifest["premarket_observations"] = premarket_observations
    manifest['supplemental_premarket_volume'] = alpaca_volume or {'diagnostic': alpaca_volume_error}
    manifest["public_source_statuses"] = public_statuses
    return freeze_evidence_bundle(
        root=root, as_of_et=completed.isoformat(),
        scheduled_cutoff_et=scheduled_cutoff.isoformat(), cutoff_status=cutoff_status,
        candidates=candidates, records=records, files=files,
        provider_manifest=manifest,
    )
