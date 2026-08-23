from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .hashing import sha256_file


class CandidateError(ValueError):
    """A deterministic candidate intake cannot be reproduced safely."""


def _returns(snapshot: dict[str, Any]) -> dict[str, tuple[float, float]]:
    output: dict[str, tuple[float, float]] = {}
    for row in snapshot.get("rows", []):
        symbol = str(row.get("symbol", "")).strip().upper()
        semantics = row.get("premarket_semantics", {})
        if semantics.get("status") != "pass":
            continue
        value = semantics.get("premarket_return")
        volume = row.get("raw_snapshot", {}).get("pre_volume")
        if not symbol or value is None:
            continue
        output[symbol] = (float(value), max(float(volume or 0), 0.0))
    return output


def select_candidates(
    *,
    stock_snapshot: dict[str, Any],
    benchmark_snapshot: dict[str, Any],
    universe_csv: Path,
    benchmark_csv: Path,
    captured_event_symbols: list[str],
    excluded_symbols: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    bindings = policy.get("parameter_bindings", {})
    for name in (
        "maximum_price_residual_candidates",
        "maximum_captured_event_candidates",
        "minimum_premarket_volume_quantile",
        "maximum_snapshot_skew_seconds",
    ):
        if len(bindings.get(name, [])) != 3:
            raise CandidateError(f"candidate policy lacks bindings for {name}")
    price_cap = int(policy["maximum_price_residual_candidates"])
    event_cap = int(policy["maximum_captured_event_candidates"])
    volume_quantile = float(policy["minimum_premarket_volume_quantile"])
    maximum_skew = int(policy["maximum_snapshot_skew_seconds"])
    if price_cap < 0 or event_cap < 0 or not 0.0 <= volume_quantile <= 1.0 or maximum_skew <= 0:
        raise CandidateError("candidate caps or participation quantile are invalid")

    for snapshot, source, name in (
        (stock_snapshot, universe_csv, "stock"),
        (benchmark_snapshot, benchmark_csv, "benchmark"),
    ):
        if snapshot.get("formal_cutoff_eligible") is not True:
            raise CandidateError(f"{name} snapshot did not pass its formal cutoff gate")
        if snapshot.get("universe", {}).get("sha256") != sha256_file(source):
            raise CandidateError(f"{name} snapshot is bound to a different input universe")
    try:
        stock_time = datetime.fromisoformat(
            str(stock_snapshot["captured_at_end_et"]).replace("Z", "+00:00")
        )
        benchmark_time = datetime.fromisoformat(
            str(benchmark_snapshot["captured_at_end_et"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateError("snapshot capture timestamps are missing or invalid") from exc
    if (
        stock_time.tzinfo is None
        or benchmark_time.tzinfo is None
        or abs((stock_time - benchmark_time).total_seconds()) > maximum_skew
    ):
        raise CandidateError("stock and benchmark snapshots are not time-aligned")

    with universe_csv.open(newline="", encoding="utf-8-sig") as handle:
        universe_rows = list(csv.DictReader(handle))
    universe = {str(row["instrument"]).strip().upper(): row for row in universe_rows}
    if not universe or len(universe) != len(universe_rows):
        raise CandidateError("effective universe is empty or duplicated")
    with benchmark_csv.open(newline="", encoding="utf-8-sig") as handle:
        benchmark_rows = list(csv.DictReader(handle))
    sector_to_etf = {
        str(row["gics_sector"]).strip(): str(row["instrument"]).strip().upper()
        for row in benchmark_rows
        if str(row.get("gics_sector", "")).strip()
    }
    sectors = {str(row.get("gics_sector", "")).strip() for row in universe_rows}
    if "" in sectors or sectors.difference(sector_to_etf):
        raise CandidateError("sector ETF mapping is incomplete")

    excluded = {str(symbol).strip().upper() for symbol in excluded_symbols}
    stock_returns = _returns(stock_snapshot)
    benchmark_returns = _returns(benchmark_snapshot)
    positive_volumes = sorted(volume for _, volume in stock_returns.values() if volume > 0)
    if positive_volumes:
        position = min(
            len(positive_volumes) - 1,
            max(0, math.ceil(volume_quantile * len(positive_volumes)) - 1),
        )
        minimum_volume = positive_volumes[position]
    else:
        minimum_volume = float("inf")
    residuals = []
    for symbol, row in universe.items():
        if symbol in excluded or symbol not in stock_returns:
            continue
        sector = str(row["gics_sector"]).strip()
        etf = sector_to_etf[sector]
        if etf not in benchmark_returns:
            continue
        stock_return, premarket_volume = stock_returns[symbol]
        if premarket_volume < minimum_volume:
            continue
        sector_return = benchmark_returns[etf][0]
        residuals.append({
            "symbol": symbol,
            "source": "absolute_stock_minus_sector_premarket_residual",
            "gics_sector": sector,
            "sector_benchmark": etf,
            "stock_premarket_return": stock_return,
            "sector_premarket_return": sector_return,
            "residual_premarket_return": stock_return - sector_return,
            "premarket_volume": premarket_volume,
        })
    price_rows = sorted(
        residuals,
        key=lambda row: (-abs(row["residual_premarket_return"]), -row["premarket_volume"], row["symbol"]),
    )[:price_cap]

    event_symbols = []
    for value in captured_event_symbols:
        symbol = str(value).strip().upper()
        if symbol not in universe:
            raise CandidateError(f"captured event symbol is outside effective universe: {symbol}")
        if symbol not in excluded and symbol not in event_symbols:
            event_symbols.append(symbol)
    event_symbols = sorted(event_symbols)[:event_cap]
    by_symbol = {row["symbol"]: dict(row) for row in price_rows}
    for symbol in event_symbols:
        if symbol in by_symbol:
            by_symbol[symbol]["captured_primary_event"] = True
        else:
            row = universe[symbol]
            by_symbol[symbol] = {
                "symbol": symbol,
                "source": "captured_primary_event",
                "gics_sector": str(row["gics_sector"]).strip(),
                "sector_benchmark": sector_to_etf[str(row["gics_sector"]).strip()],
                "captured_primary_event": True,
            }
    return {
        "schema_version": 6,
        "method": "event_plus_absolute_stock_minus_sector_residual",
        "candidates": [by_symbol[symbol] for symbol in sorted(by_symbol)],
        "excluded_symbols": sorted(excluded),
        "price_candidate_count": len(price_rows),
        "event_candidate_count": len(event_symbols),
        "participation_gate": {
            "positive_volume_observations": len(positive_volumes),
            "minimum_volume_quantile": volume_quantile,
            "observed_minimum_premarket_volume": minimum_volume if positive_volumes else None,
        },
    }
