#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6.hashing import sha256_file  # noqa: E402
from daily_oracle_v6.premarket import (  # noqa: E402
    build_symbol_snapshot_documents,
    resolve_premarket_return,
)


def _json_value(value):
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


def _symbols(path: Path, column: str, config: dict) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if column not in (reader.fieldnames or []):
            raise ValueError(f"universe is missing {column}")
        canonical = [str(row[column]).strip().upper() for row in reader if str(row[column]).strip()]
    if len(canonical) != len(set(canonical)):
        raise ValueError("universe contains duplicate symbols")
    prefix = config["provider_market_prefix"]
    source = config["class_share_input_separator"]
    target = config["class_share_provider_separator"]
    return [(symbol, prefix + symbol.replace(source, target)) for symbol in canonical]


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a cutoff-safe Futu premarket universe snapshot")
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--symbol-column", default="instrument")
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/market-data.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--split-dir",
        type=Path,
        help="Optionally freeze one exact provider-row file per symbol for lineage isolation",
    )
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")))
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError("premarket snapshot is immutable")
    if args.split_dir is not None and args.split_dir.exists():
        raise FileExistsError("per-symbol snapshot directory is immutable")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    for name in (
        "provider", "batch_size", "minimum_coverage", "minimum_semantic_pass_rate",
        "premarket_return_tolerance", "provider_market_prefix",
        "class_share_input_separator", "class_share_provider_separator",
    ):
        if len(config.get("parameter_bindings", {}).get(name, [])) != 3:
            raise ValueError(f"{name} is missing reference/decision/experiment bindings")
    cutoff = datetime.fromisoformat(args.cutoff_et.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("cutoff requires an explicit offset")
    zone = ZoneInfo("America/New_York")
    started = datetime.now(zone)
    if started > cutoff:
        raise ValueError("formal premarket cutoff has passed")
    mappings = _symbols(args.universe, args.symbol_column, config)

    from futu import OpenQuoteContext, RET_OK  # type: ignore

    quote = OpenQuoteContext(host=args.host, port=args.port)
    raw_rows: dict[str, dict] = {}
    errors: dict[str, str] = {}
    provider_to_canonical = {provider: canonical for canonical, provider in mappings}
    attempts = 0

    def fetch_batch(providers: list[str]) -> None:
        nonlocal attempts
        if not providers:
            return
        attempts += 1
        ret, frame = quote.get_market_snapshot(providers)
        if ret != RET_OK:
            if len(providers) == 1:
                errors[provider_to_canonical[providers[0]]] = str(frame)
                return
            midpoint = len(providers) // 2
            fetch_batch(providers[:midpoint])
            fetch_batch(providers[midpoint:])
            return
        returned = set()
        for _, row in frame.iterrows():
            provider = str(row["code"])
            if provider not in provider_to_canonical:
                continue
            returned.add(provider)
            raw_rows[provider_to_canonical[provider]] = {
                str(key): _json_value(value) for key, value in row.items()
            }
        missing = [provider for provider in providers if provider not in returned]
        if not missing:
            return
        if len(providers) == 1:
            errors[provider_to_canonical[providers[0]]] = "provider returned no row"
            return
        if len(missing) == 1:
            fetch_batch(missing)
            return
        midpoint = len(missing) // 2
        fetch_batch(missing[:midpoint])
        fetch_batch(missing[midpoint:])

    try:
        batch_size = int(config["batch_size"])
        for offset in range(0, len(mappings), batch_size):
            batch = mappings[offset:offset + batch_size]
            fetch_batch([provider for _, provider in batch])
    finally:
        quote.close()
    ended = datetime.now(zone)
    before_cutoff = ended <= cutoff

    rows = []
    checked = passed = 0
    for canonical, provider in mappings:
        raw = raw_rows.get(canonical)
        if raw is None:
            continue
        semantics = resolve_premarket_return(
            raw, tolerance=float(config["premarket_return_tolerance"])
        )
        if semantics["status"] in {"pass", "error"}:
            checked += 1
        if semantics["status"] == "pass":
            passed += 1
        rows.append({
            "symbol": canonical,
            "provider_symbol": provider,
            "raw_snapshot": raw,
            "premarket_semantics": semantics,
        })
    coverage = len(raw_rows) / len(mappings)
    semantic_rate = passed / checked if checked else 1.0
    formal = (
        before_cutoff
        and coverage >= float(config["minimum_coverage"])
        and semantic_rate >= float(config["minimum_semantic_pass_rate"])
    )
    payload = {
        "schema_version": 6,
        "provider": config["provider"],
        "captured_at_start_et": started.isoformat(),
        "captured_at_end_et": ended.isoformat(),
        "cutoff_et": cutoff.astimezone(zone).isoformat(),
        "formal_cutoff_eligible": formal,
        "universe": {
            "path": str(args.universe.resolve()),
            "sha256": sha256_file(args.universe),
            "requested": len(mappings),
            "returned": len(raw_rows),
            "coverage": coverage,
        },
        "semantic_audit": {
            "checked": checked,
            "passed": passed,
            "pass_rate": semantic_rate,
            "last_price_role": "forbidden_as_previous_close",
            "snapshot_prev_close_role": "diagnostic_only",
        },
        "errors": errors,
        "fetch_attempts": attempts,
        "rows": rows,
    }
    if args.split_dir is not None:
        documents = build_symbol_snapshot_documents(
            provider=config["provider"],
            captured_at_start_et=started.isoformat(),
            captured_at_end_et=ended.isoformat(),
            cutoff_et=cutoff.astimezone(zone).isoformat(),
            rows=rows,
        )
        args.split_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{args.split_dir.name}.", dir=args.split_dir.parent
        ) as temporary:
            temporary_path = Path(temporary)
            for symbol, document in documents.items():
                path = temporary_path / f"{symbol}.json"
                path.write_text(
                    json.dumps(document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            temporary_path.replace(args.split_dir)
        payload["symbol_files"] = {
            row["symbol"]: {
                "path": str((args.split_dir / f"{row['symbol']}.json").resolve()),
                "sha256": sha256_file(args.split_dir / f"{row['symbol']}.json"),
            }
            for row in rows
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "formal_cutoff_eligible": formal,
        "coverage": coverage,
        "semantic_pass_rate": semantic_rate,
        "output": str(args.output),
    }, sort_keys=True))
    return 0 if formal else 2


if __name__ == "__main__":
    raise SystemExit(main())
