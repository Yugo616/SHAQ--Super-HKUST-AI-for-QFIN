#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle import build_price_history_document  # noqa: E402
from shaq_daily_oracle.hashing import sha256_file  # noqa: E402


def _plain(value):
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze T-1 unadjusted price paths for SHAQ candidates")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--stock-snapshot", type=Path, required=True)
    parser.add_argument("--benchmark-snapshot", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/price-history.json")
    parser.add_argument("--split-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    args = parser.parse_args()
    if args.output.exists() or args.split_dir.exists():
        raise FileExistsError("price-history snapshot is immutable")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for name in ("lookback_calendar_days", "minimum_bars", "maximum_symbols", "max_count"):
        if len(config.get("parameter_bindings", {}).get(name, [])) != 3:
            raise ValueError(f"{name} requires reference, decision and experiment bindings")
    cutoff = datetime.fromisoformat(args.cutoff_et.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("cutoff requires an offset")
    now = datetime.now(ZoneInfo("America/New_York"))
    if now > cutoff:
        raise ValueError("price-history capture started after cutoff")
    end = date.fromisoformat(args.end_date)
    start = end - timedelta(days=int(config["lookback_calendar_days"]))
    candidates = json.loads(args.candidates.read_text(encoding="utf-8")).get("candidates", [])
    if len(candidates) > int(config["maximum_symbols"]):
        raise ValueError("candidate count exceeds the governed history cap")
    stocks = json.loads(args.stock_snapshot.read_text(encoding="utf-8"))
    benchmarks = json.loads(args.benchmark_snapshot.read_text(encoding="utf-8"))
    stock_rows = {row["symbol"]: row for row in stocks.get("rows", [])}
    benchmark_rows = {row["symbol"]: row for row in benchmarks.get("rows", [])}

    from futu import AuType, KLType, OpenQuoteContext, RET_OK, Session  # type: ignore

    quote = OpenQuoteContext(host=args.host, port=args.port)
    cache: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    def history(symbol: str) -> list[dict]:
        if symbol in cache:
            return cache[symbol]
        ret, frame, next_key = quote.request_history_kline(
            "US." + symbol.replace("-", "."),
            start=start.isoformat(), end=end.isoformat(),
            ktype=KLType.K_DAY, autype=AuType.NONE,
            max_count=int(config["max_count"]), extended_time=False, session=Session.RTH,
        )
        if ret != RET_OK or next_key is not None:
            raise RuntimeError(str(frame) if ret != RET_OK else "history pagination was not exhausted")
        cache[symbol] = [
            {str(key): _plain(value) for key, value in row.items()}
            for _, row in frame.iterrows()
        ]
        return cache[symbol]

    documents = {}
    try:
        for candidate in candidates:
            symbol = str(candidate["symbol"]).upper()
            sector = str(candidate.get("sector_benchmark", "")).upper()
            if not sector:
                errors[symbol] = "sector benchmark is absent from candidate context"
                continue
            try:
                if symbol not in stock_rows or sector not in benchmark_rows:
                    raise ValueError("candidate is absent from frozen premarket snapshots")
                context = dict(candidate)
                required_context = {
                    "stock_premarket_return", "sector_premarket_return",
                    "residual_premarket_return", "premarket_volume",
                }
                if not required_context.issubset(context):
                    stock_semantics = stock_rows[symbol].get("premarket_semantics", {})
                    sector_semantics = benchmark_rows[sector].get("premarket_semantics", {})
                    if stock_semantics.get("status") != "pass" or sector_semantics.get("status") != "pass":
                        raise ValueError("candidate premarket context is not semantically valid")
                    stock_return = float(stock_semantics["premarket_return"])
                    sector_return = float(sector_semantics["premarket_return"])
                    context.update({
                        "stock_premarket_return": stock_return,
                        "sector_premarket_return": sector_return,
                        "residual_premarket_return": stock_return - sector_return,
                        "premarket_volume": float(
                            stock_rows[symbol].get("raw_snapshot", {}).get("pre_volume") or 0
                        ),
                    })
                documents[symbol] = build_price_history_document(
                    symbol=symbol, sector_benchmark=sector, premarket_context=context,
                    stock_bars=history(symbol), sector_bars=history(sector),
                    end_date=end.isoformat(), minimum_bars=int(config["minimum_bars"]),
                    captured_at_et=datetime.now(ZoneInfo("America/New_York")).isoformat(),
                    cutoff_et=cutoff.isoformat(),
                )
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
    finally:
        quote.close()
    ended = datetime.now(ZoneInfo("America/New_York"))
    if ended > cutoff:
        raise ValueError("price-history capture completed after cutoff")

    args.split_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{args.split_dir.name}.", dir=args.split_dir.parent) as name:
        temporary = Path(name)
        for symbol, document in sorted(documents.items()):
            (temporary / f"{symbol}.json").write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        temporary.replace(args.split_dir)
    root = args.evidence_root.resolve()
    evidence = []
    for symbol, document in sorted(documents.items()):
        path = (args.split_dir / f"{symbol}.json").resolve()
        if root not in path.parents:
            raise ValueError("price-history evidence escapes the evidence root")
        sector = document["sector_benchmark"]
        evidence.append({
            "evidence_id": f"futu-price-history-{symbol.lower()}",
            "domain": "price_volume",
            "provider": "Futu OpenD",
            "source_uri": f"futu-opend://historical-kline/US.{symbol}",
            "raw_file_path": str(path.relative_to(root)),
            "raw_sha256": sha256_file(path),
            "captured_at": document["captured_at_et"],
            "scope_symbols": [symbol],
            "parent_evidence_ids": [
                f"futu-price_volume-{symbol.lower()}", f"futu-market-{sector.lower()}"
            ],
        })
    payload = {
        "schema_version": 6,
        "provider": "Futu OpenD",
        "captured_at_end_et": ended.isoformat(),
        "cutoff_et": cutoff.isoformat(),
        "bar_end_date": end.isoformat(),
        "adjustment": "NONE",
        "formal_cutoff_eligible": True,
        "candidate_input_sha256": sha256_file(args.candidates),
        "stock_snapshot_sha256": sha256_file(args.stock_snapshot),
        "benchmark_snapshot_sha256": sha256_file(args.benchmark_snapshot),
        "evidence": evidence,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"eligible_records": len(evidence), "unavailable": len(errors)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
