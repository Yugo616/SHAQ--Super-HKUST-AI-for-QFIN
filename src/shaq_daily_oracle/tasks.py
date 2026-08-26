from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .contracts import DOMAINS
from .hashing import sha256_payload


class TaskError(ValueError):
    """A domain task would expose another domain or an answer field."""


FORBIDDEN_TASK_KEYS = {
    "prediction",
    "predictions",
    "direction",
    "core_direction",
    "ranking",
    "rank",
    "score",
    "label",
    "labels",
    "outcome",
    "next_return",
    "official_close",
    "correct",
    "pnl",
    "other_agent_reports",
    "domain_reports",
}

DOMAIN_EVIDENCE_ROUTES = {
    "market": {"market", "relationships"},
    "relationships": {"relationships", "event", "price_volume", "market"},
    "event": {"event", "price_volume"},
    "capital": {"capital", "price_volume"},
    "derivatives": {"derivatives", "price_volume"},
    "price_volume": {"price_volume", "market", "relationships", "event"},
}


def _scan_keys(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_TASK_KEYS:
                raise TaskError(f"forbidden task field {key!r} at {location}")
            _scan_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_keys(child, f"{location}[{index}]")


def _scan_json_source(path: Path) -> None:
    if path.suffix.lower() not in {".json", ".jsonl"}:
        return
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                _scan_keys(json.loads(line), f"{path.name}:{line_number}")
        return
    _scan_keys(json.loads(text), path.name)


def build_blind_domain_tasks(
    *,
    lineage: dict[str, Any],
    symbols: Iterable[str],
    as_of_et: str,
    horizon: str,
    collection_statuses: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    records = list(lineage.get("records", []))
    by_domain: dict[str, list[dict[str, Any]]] = {domain: [] for domain in DOMAINS}
    for record in records:
        domain = record.get("domain")
        if domain not in DOMAINS:
            raise TaskError(f"unsupported evidence domain: {domain}")
        source_path = Path(record["raw_file_path"])
        _scan_json_source(source_path)
        public_record = {
            "evidence_id": record["evidence_id"],
            "provider": record["provider"],
            "source_uri": record["source_uri"],
            "raw_file_path": str(source_path),
            "raw_sha256": record["raw_sha256"],
            "captured_at": record["captured_at"],
            "lineage_root_ids": record["lineage_root_ids"],
            "source_domain": domain,
        }
        if record.get("analysis_file_path"):
            analysis_path = Path(record["analysis_file_path"])
            _scan_json_source(analysis_path)
            public_record.update({
                "analysis_file_path": str(analysis_path),
                "analysis_sha256": record["analysis_sha256"],
                "analysis_transform": record["analysis_transform"],
            })
        scopes = record.get("scope_symbols")
        if scopes is not None:
            public_record["scope_symbols"] = sorted(str(value).upper() for value in scopes)
        consumers = set(record.get("consumer_domains", [domain]))
        if not consumers or not consumers.issubset(DOMAINS):
            raise TaskError("evidence consumer domains are invalid")
        for consumer in sorted(consumers):
            by_domain[consumer].append(public_record)

    status_rows = [dict(row) for row in collection_statuses]
    tasks = []
    for symbol in sorted({str(value).upper() for value in symbols}):
        for domain in sorted(DOMAINS):
            evidence = [
                record
                for record in by_domain[domain]
                if "scope_symbols" not in record
                or "*" in record["scope_symbols"]
                or symbol in record["scope_symbols"]
            ]
            seed = {
                "symbol": symbol,
                "domain": domain,
                "as_of_et": as_of_et,
                "horizon": horizon,
                "evidence": sorted(evidence, key=lambda item: item["evidence_id"]),
            }
            relevant = [
                row for row in status_rows
                if str(row.get("domain")) == domain
                and str(row.get("symbol", "")).upper() in {symbol, "*"}
            ]
            own_evidence = [row for row in evidence if row.get("source_domain") == domain]
            if own_evidence:
                status = "collected"
            elif relevant:
                priority = {"not_entitled": 4, "provider_error": 3, "no_data": 2, "not_applicable": 1}
                status = max(
                    (str(row.get("status")) for row in relevant),
                    key=lambda value: priority.get(value, 0),
                )
            else:
                status = "not_applicable" if domain == "event" else "no_data"
            seed["collection_status"] = {
                "status": status,
                "details": sorted(
                    [{"status": row.get("status"), "reason": row.get("reason")} for row in relevant],
                    key=lambda row: (str(row["status"]), str(row["reason"])),
                ),
            }
            tasks.append({"task_id": "task_" + sha256_payload(seed)[:20], **seed})
    return {
        "schema_version": 6,
        "as_of_et": as_of_et,
        "horizon": horizon,
        "tasks": tasks,
    }
