from __future__ import annotations

import csv
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .collectors import (
    CollectorError,
    build_capital_document,
    build_derivatives_document,
    build_relationship_document,
    collection_status,
)
from .hashing import sha256_file


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _write_immutable(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"immutable deep-evidence artifact exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record(
    *, domain: str, symbol: str, path: Path, evidence_root: Path,
    captured_at: str, source_uri: str, parent_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    record = {
        "evidence_id": f"futu-{domain}-{symbol.lower()}",
        "domain": domain,
        "provider": "Futu OpenD" if domain != "relationships" else "PIT universe and Futu T-1 history",
        "source_uri": source_uri,
        "raw_file_path": str(path.resolve().relative_to(evidence_root.resolve())),
        "raw_sha256": sha256_file(path),
        "captured_at": captured_at,
        "scope_symbols": [symbol],
    }
    if parent_evidence_ids:
        record["parent_evidence_ids"] = parent_evidence_ids
    return record


def _directions(frame: Any) -> list[dict[str, Any]]:
    rows = []
    for _, row in frame.iterrows():
        value = str(row.get("ticker_direction", "")).upper()
        direction = next((name for name in ("BUY", "SELL", "NEUTRAL") if value.endswith(name)), value)
        rows.append({
            "time": _plain(row.get("time")),
            "price": _plain(row.get("price")),
            "volume": _plain(row.get("volume")),
            "ticker_direction": direction,
        })
    return rows


def _book(document: dict[str, Any], observed_at: str) -> dict[str, Any]:
    def levels(name: str) -> list[dict[str, Any]]:
        output = []
        for row in document.get(name, []):
            if len(row) >= 2:
                output.append({"price": _plain(row[0]), "volume": _plain(row[1])})
        return output
    return {"observed_at_et": observed_at, "bid": levels("Bid"), "ask": levels("Ask")}


def _option_rows(chain: Any, snapshots: Any) -> list[dict[str, Any]]:
    chain_by_code = {str(row["code"]): row.to_dict() for _, row in chain.iterrows()}
    output = []
    for _, snapshot in snapshots.iterrows():
        code = str(snapshot["code"])
        base = chain_by_code.get(code)
        if not base:
            continue
        output.append({
            "code": code,
            "option_type": _plain(base.get("option_type")),
            "strike_time": _plain(base.get("strike_time")),
            "strike_price": _plain(base.get("strike_price")),
            "bid_price": _plain(snapshot.get("bid_price")),
            "ask_price": _plain(snapshot.get("ask_price")),
            "option_implied_volatility": _plain(snapshot.get("option_implied_volatility")),
            "option_open_interest": _plain(snapshot.get("option_open_interest")),
            "volume": _plain(snapshot.get("volume")),
        })
    return output


def capture_futu_deep_evidence(
    *, candidates: list[dict[str, Any]], universe_csv: Path,
    price_history_dir: Path, evidence_root: Path, output_manifest: Path,
    status_output: Path, cutoff_et: str, config: dict[str, Any],
    host: str, port: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture the three structurally missing domains, failing each provider independently."""

    now = datetime.now(ZoneInfo("America/New_York"))
    cutoff = datetime.fromisoformat(cutoff_et.replace("Z", "+00:00"))
    if now > cutoff:
        raise CollectorError("deep evidence capture started after cutoff")
    for name in (
        "order_book_depth", "order_book_samples", "sample_interval_seconds",
        "ticker_max_count", "capital_window_start", "relationship_exposure_window", "option_expiry_min_days",
        "option_expiry_max_days", "option_max_contracts",
    ):
        if len(config.get("parameter_bindings", {}).get(name, [])) != 3:
            raise CollectorError(f"deep-evidence config lacks bindings for {name}")
    with universe_csv.open(newline="", encoding="utf-8-sig") as handle:
        universe_rows = list(csv.DictReader(handle))

    from futu import OpenQuoteContext, RET_OK, SubType  # type: ignore

    quote = OpenQuoteContext(host=host, port=port)
    records: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    try:
        for candidate in candidates:
            symbol = str(candidate["symbol"]).upper()
            code = f"US.{symbol}"
            captured = datetime.now(ZoneInfo("America/New_York")).isoformat()

            history_path = price_history_dir / f"{symbol}.json"
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                relationship = build_relationship_document(
                    symbol=symbol, universe_rows=universe_rows, price_history=history,
                    captured_at_et=captured,
                    exposure_window=int(config["relationship_exposure_window"]),
                )
                relationship_captured = datetime.now(ZoneInfo("America/New_York")).isoformat()
                relationship["captured_at_et"] = relationship_captured
                path = evidence_root / "relationships_by_symbol" / f"{symbol}.json"
                _write_immutable(path, relationship)
                records.append(_record(
                    domain="relationships", symbol=symbol, path=path,
                    evidence_root=evidence_root, captured_at=relationship_captured,
                    source_uri=f"daily-oracle://pit-relationship/{symbol}",
                    parent_evidence_ids=[f"futu-price-history-{symbol.lower()}"],
                ))
                statuses.append(collection_status(
                    domain="relationships", symbol=symbol, status="collected",
                    captured_at_et=relationship_captured, record_count=1,
                ))
            except (CollectorError, FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as exc:
                statuses.append(collection_status(
                    domain="relationships", symbol=symbol, status="no_data",
                    captured_at_et=captured, reason=str(exc),
                ))

            try:
                ret, error = quote.subscribe([code], [SubType.TICKER, SubType.ORDER_BOOK], is_first_push=False)
                if ret != RET_OK:
                    message = str(error)
                    status = "not_entitled" if "right" in message.lower() or "authority" in message.lower() else "provider_error"
                    statuses.append(collection_status(
                        domain="capital", symbol=symbol, status=status,
                        captured_at_et=captured, reason=message,
                    ))
                else:
                    ret, ticker_frame = quote.get_rt_ticker(code, num=int(config["ticker_max_count"]))
                    if ret != RET_OK:
                        raise RuntimeError(str(ticker_frame))
                    books = []
                    for index in range(int(config["order_book_samples"])):
                        ret, order_book = quote.get_order_book(code, num=int(config["order_book_depth"]))
                        if ret != RET_OK:
                            raise RuntimeError(str(order_book))
                        books.append(_book(order_book, datetime.now(ZoneInfo("America/New_York")).isoformat()))
                        if index + 1 < int(config["order_book_samples"]):
                            time.sleep(float(config["sample_interval_seconds"]))
                    capital_captured = datetime.now(ZoneInfo("America/New_York"))
                    capital_window_start = datetime.combine(
                        capital_captured.date(),
                        datetime.strptime(str(config["capital_window_start"]), "%H:%M:%S").time(),
                        ZoneInfo("America/New_York"),
                    )
                    capital = build_capital_document(
                        symbol=symbol, ticker_rows=_directions(ticker_frame),
                        order_book_samples=books, captured_at_et=capital_captured.isoformat(),
                        window_start_et=capital_window_start.isoformat(), window_end_et=cutoff.isoformat(),
                    )
                    path = evidence_root / "capital_by_symbol" / f"{symbol}.json"
                    _write_immutable(path, capital)
                    records.append(_record(
                        domain="capital", symbol=symbol, path=path,
                        evidence_root=evidence_root, captured_at=capital_captured.isoformat(),
                        source_uri=f"futu-opend://ticker-orderbook/{code}",
                    ))
                    statuses.append(collection_status(
                        domain="capital", symbol=symbol, status="collected",
                        captured_at_et=capital_captured.isoformat(), record_count=len(capital["ticker_rows"]) + len(capital["order_book_samples"]),
                    ))
            except CollectorError as exc:
                statuses.append(collection_status(
                    domain="capital", symbol=symbol, status="no_data",
                    captured_at_et=captured, reason=str(exc),
                ))
            except Exception as exc:
                statuses.append(collection_status(
                    domain="capital", symbol=symbol, status="provider_error",
                    captured_at_et=captured, reason=f"{type(exc).__name__}: {exc}",
                ))

            try:
                ret, underlying = quote.get_market_snapshot([code])
                if ret != RET_OK or underlying.empty:
                    raise RuntimeError(str(underlying))
                spot = float(underlying.iloc[0]["last_price"])
                ret, expiry_frame = quote.get_option_expiration_date(code)
                if ret != RET_OK:
                    raise RuntimeError(str(expiry_frame))
                eligible_expiries = sorted(
                    str(row["strike_time"])
                    for _, row in expiry_frame.iterrows()
                    if int(config["option_expiry_min_days"]) <= int(row["option_expiry_date_distance"]) <= int(config["option_expiry_max_days"])
                )[:2]
                chains = []
                for expiry in eligible_expiries:
                    ret, chain = quote.get_option_chain(code, start=expiry, end=expiry)
                    if ret == RET_OK and not chain.empty:
                        chains.append(chain)
                if not chains:
                    statuses.append(collection_status(
                        domain="derivatives", symbol=symbol, status="not_applicable",
                        captured_at_et=captured, reason="no eligible listed option contracts",
                    ))
                else:
                    import pandas as pd  # type: ignore
                    chain = pd.concat(chains, ignore_index=True)
                    chain = chain.iloc[:int(config["option_max_contracts"])]
                    codes = [str(value) for value in chain["code"]]
                    frames = []
                    for offset in range(0, len(codes), 200):
                        ret, frame = quote.get_market_snapshot(codes[offset:offset + 200])
                        if ret != RET_OK:
                            raise RuntimeError(str(frame))
                        frames.append(frame)
                    snapshots = pd.concat(frames, ignore_index=True)
                    derivatives_captured = datetime.now(ZoneInfo("America/New_York")).isoformat()
                    derivatives = build_derivatives_document(
                        symbol=symbol, underlying_price=spot,
                        option_rows=_option_rows(chain, snapshots), captured_at_et=derivatives_captured,
                    )
                    path = evidence_root / "derivatives_by_symbol" / f"{symbol}.json"
                    _write_immutable(path, derivatives)
                    records.append(_record(
                        domain="derivatives", symbol=symbol, path=path,
                        evidence_root=evidence_root, captured_at=derivatives_captured,
                        source_uri=f"futu-opend://option-chain/{code}",
                    ))
                    statuses.append(collection_status(
                        domain="derivatives", symbol=symbol, status="collected",
                        captured_at_et=derivatives_captured, record_count=len(codes),
                    ))
            except CollectorError as exc:
                statuses.append(collection_status(
                    domain="derivatives", symbol=symbol, status="no_data",
                    captured_at_et=captured, reason=str(exc),
                ))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                status = "not_entitled" if "right" in message.lower() or "authority" in message.lower() else "provider_error"
                statuses.append(collection_status(
                    domain="derivatives", symbol=symbol, status=status,
                    captured_at_et=captured, reason=message,
                ))
    finally:
        quote.close()
    ended = datetime.now(ZoneInfo("America/New_York"))
    if ended > cutoff:
        raise CollectorError("deep evidence capture completed after cutoff")
    manifest = {"schema_version": 6, "evidence": sorted(records, key=lambda row: row["evidence_id"])}
    status_document = {
        "schema_version": 6,
        "captured_at_start_et": now.isoformat(),
        "captured_at_end_et": ended.isoformat(),
        "cutoff_et": cutoff.isoformat(),
        "statuses": sorted(statuses, key=lambda row: (row["symbol"], row["domain"])),
    }
    _write_immutable(output_manifest, manifest)
    _write_immutable(status_output, status_document)
    return manifest, status_document
