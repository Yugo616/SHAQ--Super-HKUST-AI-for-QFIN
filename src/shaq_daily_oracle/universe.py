from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload


class UniverseError(ValueError):
    """An effective-universe derivation cannot be reproduced safely."""


def _normalized_source_text(payload: bytes) -> str:
    text = html.unescape(payload.decode("utf-8", errors="replace"))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _receipt(path: Path, raw_path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    claimed = value.get("receipt_sha256")
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if claimed != sha256_payload(unsigned):
        raise UniverseError("source receipt hash is invalid")
    if value.get("raw_sha256") != sha256_file(raw_path):
        raise UniverseError("official source bytes do not match their receipt")
    return value


def _as_utc_z(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UniverseError("receipt timestamp needs an explicit offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def derive_effective_universe(
    *,
    base_csv: Path,
    source_path: Path,
    receipt_path: Path,
    events: list[dict[str, Any]],
    as_of: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    as_of_date = date.fromisoformat(as_of)
    receipt = _receipt(receipt_path, source_path)
    source_text = _normalized_source_text(source_path.read_bytes())
    with base_csv.open(newline="", encoding="utf-8-sig") as handle:
        base_rows = list(csv.DictReader(handle))
    if not base_rows or "ticker" not in base_rows[0]:
        raise UniverseError("base membership must contain ticker rows")
    by_ticker = {str(row["ticker"]).strip().upper(): dict(row) for row in base_rows}
    if len(by_ticker) != len(base_rows) or "" in by_ticker:
        raise UniverseError("base membership contains blank or duplicate tickers")

    applied: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda value: (value["effective_date"], value["action"], value["ticker"])):
        effective = date.fromisoformat(str(event["effective_date"]))
        if effective > as_of_date:
            continue
        ticker = str(event["ticker"]).strip().upper()
        action = str(event["action"]).strip().lower()
        if action not in {"addition", "deletion"} or not ticker:
            raise UniverseError("membership event action or ticker is invalid")
        assertions = event.get("source_assertions")
        if not isinstance(assertions, list) or not assertions:
            raise UniverseError("membership event needs exact official-source assertions")
        if any(str(assertion) not in source_text for assertion in assertions):
            raise UniverseError("membership event is not supported by the frozen official source")
        if action == "deletion":
            if ticker not in by_ticker:
                raise UniverseError(f"cannot delete absent constituent {ticker}")
            by_ticker.pop(ticker)
        else:
            if ticker in by_ticker:
                raise UniverseError(f"cannot add existing constituent {ticker}")
            company = str(event.get("company_name", "")).strip()
            sector = str(event.get("gics_sector", "")).strip()
            if not company or not sector:
                raise UniverseError("addition needs company_name and gics_sector")
            by_ticker[ticker] = {
                "ticker": ticker,
                "company_name": company,
                "gics_sector": sector,
                "gics_sub_industry": str(event.get("gics_sub_industry", "")).strip(),
                "cik_company_id": str(event.get("cik_company_id", "")).strip(),
                "stable_security_id": str(event.get("stable_security_id", "")).strip(),
                "listing_id": str(event.get("listing_id", "")).strip(),
                "source_role": "official_membership_event",
                "source_url": str(receipt["final_uri"]),
                "observed_active_at_utc": _as_utc_z(str(receipt["captured_at_end_et"])),
                "known_from_utc": _as_utc_z(str(receipt["captured_at_end_et"])),
            }
        applied.append({
            "effective_date": effective.isoformat(),
            "action": action,
            "ticker": ticker,
            "event_id": str(event.get("event_id", "")),
        })

    output = []
    for ticker in sorted(by_ticker):
        row = dict(by_ticker[ticker])
        row["ticker"] = ticker
        row["instrument"] = ticker
        output.append(row)
    manifest = {
        "schema_version": 6,
        "as_of_date": as_of_date.isoformat(),
        "method": "immutable_base_plus_effective_official_events",
        "base_path": str(base_csv.resolve()),
        "base_sha256": sha256_file(base_csv),
        "source_path": str(source_path.resolve()),
        "source_sha256": sha256_file(source_path),
        "source_receipt_path": str(receipt_path.resolve()),
        "source_receipt_file_sha256": sha256_file(receipt_path),
        "source_receipt_sha256": receipt["receipt_sha256"],
        "base_count": len(base_rows),
        "base_source_roles": sorted({str(row.get("source_role", "")) for row in base_rows}),
        "effective_count": len(output),
        "applied_events": applied,
    }
    manifest["derivation_sha256"] = hashlib.sha256(
        json.dumps(
            {"rows": output, "manifest": manifest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return output, manifest
