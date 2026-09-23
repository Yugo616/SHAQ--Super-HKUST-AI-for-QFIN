"""Prepare prior daily history without collecting today's premarket or running models."""

from __future__ import annotations

import math
import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from .data_providers import (
    DataProviderError, YFinanceProvider, load_versioned_universe,
)
from .hashing import sha256_file
from .market_calendar import _nyse, market_session, previous_market_session
from .public_data import DailyBarCache
from .research_collection import _benchmark_rows, history_lookback_days
from .settings import _atomic_json


ET = ZoneInfo("America/New_York")


def _wall_now() -> datetime:
    return datetime.now(ET)


def _monotonic() -> float:
    return time.monotonic()


def warm_previous_daily_bars(
    provider,
    symbols: list[str],
    *,
    start: date,
    target_session_date: date,
    chunk_size: int,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Warm an injected daily cache through T-1; never request the target session.

    The caller owns provider construction and bounds the in-flight provider call
    with its remaining deadline. This function checks cancellation and deadline
    before and after each batch, and does not turn a partial warmup into success.
    """
    if (start >= target_session_date or type(chunk_size) is not int or chunk_size <= 0
            or not math.isfinite(deadline_monotonic)
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("invalid prior daily history preparation request")

    completed = collected = no_data = 0

    def result(status: str, **details) -> dict:
        return {
            "status": status,
            "requested_count": len(symbols),
            "completed_count": completed,
            "collected_count": collected,
            "no_data_count": no_data,
            "pending_symbols": symbols[completed:],
            **details,
        }

    for offset in range(0, len(symbols), chunk_size):
        if cancelled():
            return result("cancelled")
        if monotonic() >= deadline_monotonic:
            return result("deadline_exceeded")
        group = symbols[offset:offset + chunk_size]
        try:
            rows = provider.history(group, start=start, end=target_session_date,
                                    interval="1d", prepost=False)
        except DataProviderError as exc:
            if exc.diagnostic.get("kind") == "deadline_exceeded":
                return result("deadline_exceeded")
            event = {"status": "batch_failed", "symbols": group,
                     "kind": exc.diagnostic.get("kind", "provider_error")}
            if progress is not None:
                progress(event)
            return result("provider_error", failed_batch=group,
                          failure_kind=event["kind"])
        if cancelled():
            return result("cancelled")
        if monotonic() >= deadline_monotonic:
            return result("deadline_exceeded")
        if not isinstance(rows, dict) or set(rows) - set(group):
            return result("provider_error", failed_batch=group,
                          failure_kind="protocol_error")
        for symbol in group:
            status = "collected" if rows.get(symbol) else "no_data"
            completed += 1
            if status == "collected":
                collected += 1
            else:
                no_data += 1
            if progress is not None:
                progress({"status": status, "symbol": symbol,
                          "completed_count": completed,
                          "requested_count": len(symbols)})
    return result("completed")


def prepare_research_history(paths, profile, *, now, deadline_et, observer=None):
    """Warm versioned T-1 daily bars; never collect current premarket evidence.

    The caller decides whether the app is writable and when to invoke this
    explicit operation. It reads no credentials and starts no background work.
    """
    if now.tzinfo is None or deadline_et.tzinfo is None:
        raise ValueError("preparation times must be timezone-aware")
    now_et, deadline_et = now.astimezone(ET), deadline_et.astimezone(ET)
    identity = profile.identity()
    base = {"session_date": now_et.date().isoformat(),
            "profile_sha256": identity, "requested_count": 0,
            "completed_count": 0, "collected_count": 0,
            "no_data_count": 0, "pending_symbols": []}
    if (profile.market_provider != "yfinance" or deadline_et <= now_et
            or market_session(now_et.date()) is None):
        return {**base, "status": "not_applicable"}

    config_path = paths.package_root / "config/research-preparation.json"
    public_path = paths.package_root / "config/public-data.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    public = json.loads(public_path.read_text(encoding="utf-8"))
    lead_minutes = config["lead_minutes"]
    lookback_days = history_lookback_days(paths.package_root)
    guard_seconds = config["deadline_guard_seconds"]
    lock_timeout = config["lock_timeout_seconds"]
    overlap_days = public["history_overlap_days"]
    if (type(lead_minutes) is not int or lead_minutes <= 0
            or type(lookback_days) is not int or lookback_days <= 0
            or type(guard_seconds) is not int or guard_seconds < 0
            or type(lock_timeout) not in {int, float} or lock_timeout < 0
            or type(overlap_days) is not int or overlap_days < 0):
        raise ValueError("invalid research preparation configuration")

    universe_path = Path(profile.universe_file)
    if not universe_path.is_absolute():
        universe_path = paths.package_root / universe_path
    universe_path = universe_path.resolve()
    package_root = paths.package_root.resolve()
    # PyInstaller's macOS bundle links Frameworks/config to Resources/config.
    # Accept shipped configuration after resolving that link, but not a CSV
    # symlink escaping either of the versioned resource roots.
    config_root = (package_root / "config").resolve()
    if not (universe_path.is_relative_to(package_root)
            or universe_path.is_relative_to(config_root)):
        raise ValueError("preparation requires a versioned package universe")
    benchmark_path = package_root / "config/market-benchmarks.csv"
    members = load_versioned_universe(universe_path, cutoff=now_et)
    benchmark_symbols, _ = _benchmark_rows(benchmark_path)
    symbols = sorted({member.symbol for member in members} | set(benchmark_symbols))
    start = now_et.date() - timedelta(days=lookback_days)
    source_identity = profile.history_identity()
    expected_sessions = tuple(
        stamp.date().isoformat() for stamp in _nyse().schedule(
            start_date=start.isoformat(),
            end_date=(now_et.date() - timedelta(days=1)).isoformat(),
        ).index
    )
    previous_session = previous_market_session(now_et.date()).session_date.isoformat()
    expected_set = set(expected_sessions)

    def history_quality(cached: dict) -> dict:
        valid_dates = set()
        for row in cached.get("rows", []):
            try:
                observed = datetime.fromisoformat(str(row["timestamp"])).date().isoformat()
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(close) and close > 0 and observed in expected_set:
                valid_dates.add(observed)
        missing = expected_set - valid_dates
        if not cached:
            reason = "no_verified_cache"
        elif cached["start"] > start.isoformat() or cached["end"] < now_et.date().isoformat():
            reason = "request_window_not_covered"
        elif previous_session not in expected_set or previous_session not in valid_dates:
            reason = "missing_previous_session"
        elif missing and valid_dates and all(day < min(valid_dates) for day in missing):
            reason = "leading_history_gap"
        elif missing:
            reason = "missing_sessions"
        else:
            reason = None
        return {
            "complete": reason is None,
            "reason": reason,
            "expected_session_count": len(expected_sessions),
            "observed_session_count": len(valid_dates),
            "missing_session_count": len(missing),
            "previous_session": previous_session,
            "previous_session_present": previous_session in valid_dates,
            "first_observed_session": min(valid_dates) if valid_dates else None,
            "listing_start_unverified": reason == "leading_history_gap",
        }

    state_root = paths.research_root / "preparation"
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / f"{now_et.date().isoformat()}-{identity}.json"
    lock = FileLock(str(state_path) + ".lock", timeout=lock_timeout)
    state = {**base, "requested_count": len(symbols),
             "pending_symbols": symbols, "state_path": str(state_path),
             "history_source_identity": source_identity,
             "universe_sha256": sha256_file(universe_path),
             "benchmark_sha256": sha256_file(benchmark_path),
             "preparation_config_sha256": sha256_file(config_path),
             "public_config_sha256": sha256_file(public_path),
             "requested_start": start.isoformat(),
             "complete_history_count": 0,
             "incomplete_symbols": [], "history_quality_by_symbol": {}}

    try:
        with lock:
            cache = DailyBarCache(None, paths.research_root / "cache/daily_bars"
                                  / source_identity, overlap_days=overlap_days)
            reusable = []
            pending = []
            quality_by_symbol = {}
            for symbol in symbols:
                cached = cache._read(symbol)
                quality = history_quality(cached)
                quality_by_symbol[symbol] = quality
                if quality["complete"]:
                    reusable.append(symbol)
                else:
                    pending.append(symbol)
            state.update(completed_count=len(reusable), collected_count=len(reusable),
                         complete_history_count=len(reusable),
                         pending_symbols=pending, history_quality_by_symbol=quality_by_symbol)
            if not pending:
                state.update(status="already_completed", incomplete_symbols=[],
                             updated_at_et=_wall_now().isoformat())
                _atomic_json(state_path, state)
                return dict(state)

            try:
                previous_state = json.loads(state_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError, OSError):
                previous_state = {}
            if (previous_state.get("status") in {"completed_with_gaps", "already_checked_incomplete"}
                    and not any(quality_by_symbol[symbol]["reason"] == "missing_previous_session"
                                for symbol in pending)
                    and previous_state.get("history_source_identity") == source_identity
                    and previous_state.get("universe_sha256") == state["universe_sha256"]
                    and previous_state.get("benchmark_sha256") == state["benchmark_sha256"]
                    and previous_state.get("preparation_config_sha256") == state["preparation_config_sha256"]
                    and previous_state.get("public_config_sha256") == state["public_config_sha256"]
                    and previous_state.get("requested_start") == state["requested_start"]
                    and previous_state.get("incomplete_symbols") == pending):
                state.update(status="already_checked_incomplete",
                             completed_count=len(symbols),
                             collected_count=previous_state.get("collected_count", len(reusable)),
                             no_data_count=previous_state.get("no_data_count", 0),
                             incomplete_symbols=pending, pending_symbols=[],
                             updated_at_et=_wall_now().isoformat())
                _atomic_json(state_path, state)
                return dict(state)

            monotonic_deadline = _monotonic() + (deadline_et - now_et).total_seconds()

            class BudgetedDailyProvider:
                def history(self, group, *, start, end, interval="1d", prepost=False):
                    remaining = min(
                        (deadline_et - _wall_now()).total_seconds(),
                        monotonic_deadline - _monotonic(),
                    ) - guard_seconds
                    timeout = math.floor(remaining)
                    if timeout < 1:
                        raise DataProviderError("daily preparation deadline reached",
                                                diagnostic={"kind": "deadline_exceeded"})
                    bounded = replace(
                        profile,
                        yahoo_worker_timeout_seconds=min(
                            profile.yahoo_worker_timeout_seconds, timeout),
                        request_timeout_seconds=min(profile.request_timeout_seconds, timeout),
                    )
                    market = YFinanceProvider(bounded)
                    market.history_checkpoint_root = (
                        paths.research_root / "cache/collection_requests"
                        / now_et.date().isoformat() / source_identity)
                    market.history_source_identity = source_identity
                    return market.history(group, start=start, end=end,
                                          interval=interval, prepost=prepost)

            cache.provider = BudgetedDailyProvider()
            state.update(status="running", updated_at_et=_wall_now().isoformat())
            _atomic_json(state_path, state)

            def progress(event):
                if "symbol" in event:
                    symbol = event["symbol"]
                    quality_by_symbol[symbol] = history_quality(cache._read(symbol))
                    state["completed_count"] = len(reusable) + event["completed_count"]
                    state["pending_symbols"] = pending[event["completed_count"]:]
                    state["complete_history_count"] = sum(
                        row["complete"] for row in quality_by_symbol.values()
                    )
                    state["incomplete_symbols"] = [
                        symbol for symbol in reusable + pending[:event["completed_count"]]
                        if not quality_by_symbol[symbol]["complete"]
                    ]
                    if event["status"] == "collected":
                        state["collected_count"] += 1
                    else:
                        state["no_data_count"] += 1
                state["last_event"] = event
                state["updated_at_et"] = _wall_now().isoformat()
                _atomic_json(state_path, state)
                if observer is not None:
                    try:
                        observer({**event, "completed_count": state["completed_count"],
                                  "requested_count": len(symbols)})
                    except Exception:
                        pass  # Progress presentation cannot alter data collection.

            outcome = warm_previous_daily_bars(
                cache, pending, start=start, target_session_date=now_et.date(),
                chunk_size=profile.batch_size,
                deadline_monotonic=monotonic_deadline,
                monotonic=_monotonic,
                cancelled=lambda: _wall_now() >= deadline_et,
                progress=progress,
            )
            checked = reusable + pending[:outcome["completed_count"]]
            complete_history_count = sum(
                quality_by_symbol[symbol]["complete"] for symbol in checked
            )
            if outcome["status"] == "completed" and complete_history_count < len(symbols):
                outcome["status"] = "completed_with_gaps"
            state.update(outcome, requested_count=len(symbols),
                         completed_count=len(reusable) + outcome["completed_count"],
                         collected_count=len(reusable) + outcome["collected_count"],
                         complete_history_count=complete_history_count,
                         incomplete_symbols=[symbol for symbol in checked
                                             if not quality_by_symbol[symbol]["complete"]],
                         history_quality_by_symbol=quality_by_symbol,
                         updated_at_et=_wall_now().isoformat())
            _atomic_json(state_path, state)
            return dict(state)
    except Timeout:
        return {**state, "status": "already_running"}
