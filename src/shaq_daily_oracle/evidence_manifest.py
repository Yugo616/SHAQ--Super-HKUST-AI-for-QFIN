from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .hashing import sha256_file, sha256_payload
from .capture import CaptureError, build_capture_receipt, verify_publication_proof
from .sec_view import SecViewError, verify_sec_view_receipt


class ManifestError(ValueError):
    """A provider snapshot cannot be converted into frozen evidence safely."""


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("snapshot capture time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError("snapshot capture time requires an offset")
    return parsed


def _records(
    *, snapshot: dict[str, Any], evidence_root: Path, domain: str, scope_all: bool,
    upstream_channels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if snapshot.get("formal_cutoff_eligible") is not True:
        raise ManifestError("snapshot is not formal-cutoff eligible")
    rows = {str(row.get("symbol", "")).upper(): row for row in snapshot.get("rows", [])}
    files = snapshot.get("symbol_files", {})
    if "" in rows or set(rows) != set(files):
        raise ManifestError("snapshot rows and per-symbol files differ")
    capture_time = str(snapshot.get("captured_at_end_et", ""))
    cutoff = str(snapshot.get("cutoff_et", ""))
    if _time(capture_time) > _time(cutoff):
        raise ManifestError("snapshot was captured after cutoff")
    output = []
    root = evidence_root.resolve()
    for symbol in sorted(rows):
        declared = files[symbol]
        path = Path(str(declared.get("path", ""))).resolve()
        if root not in path.parents or not path.is_file():
            raise ManifestError("per-symbol evidence escapes the evidence root or is missing")
        actual = sha256_file(path)
        if actual != declared.get("sha256"):
            raise ManifestError("per-symbol evidence SHA-256 mismatch")
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            str(document.get("symbol", "")).upper() != symbol
            or str(document.get("captured_at_end_et", "")) != capture_time
        ):
            raise ManifestError("per-symbol evidence identity or capture time changed")
        record = {
            "evidence_id": f"futu-{domain}-{symbol.lower()}",
            "domain": domain,
            "provider": str(snapshot.get("provider", "Futu OpenD")),
            "source_uri": f"futu-opend://market-snapshot/{document['provider_symbol']}",
            "raw_file_path": str(path.relative_to(root)),
            "raw_sha256": actual,
            "captured_at": capture_time,
            "scope_symbols": ["*"] if scope_all else [symbol],
            "root_component_type": "market_context" if scope_all else "stock_price_volume",
            "consumer_domains": (
                ["market", "price_volume", "relationships"] if scope_all
                else ["price_volume", "event", "capital", "derivatives", "relationships"]
            ),
        }
        if upstream_channels is not None:
            channel = str(upstream_channels.get(symbol, "")).strip()
            if not channel:
                raise ManifestError(f"{symbol} lacks a governed lineage channel")
            if channel == "sector_breadth":
                record["root_component_type"] = "industry_context"
        output.append(record)
    return output


def build_snapshot_evidence_manifest(
    *, stock_snapshot: dict[str, Any], benchmark_snapshot: dict[str, Any], evidence_root: Path,
    benchmark_channels: dict[str, str],
) -> dict[str, Any]:
    evidence = _records(
        snapshot=stock_snapshot, evidence_root=evidence_root,
        domain="price_volume", scope_all=False,
    ) + _records(
        snapshot=benchmark_snapshot, evidence_root=evidence_root,
        domain="market", scope_all=True, upstream_channels=benchmark_channels,
    )
    ids = [row["evidence_id"] for row in evidence]
    if len(ids) != len(set(ids)):
        raise ManifestError("evidence IDs are duplicated")
    return {"schema_version": 6, "evidence": evidence}


def build_primary_event_record(
    *, symbol: str, raw_file: Path, receipt_file: Path, evidence_root: Path,
    provider: str = "issuer-primary", analysis_file: Path | None = None,
    analysis_receipt_file: Path | None = None,
) -> dict[str, Any]:
    canonical = symbol.strip().upper()
    if not canonical or not canonical.replace("-", "").replace(".", "").isalnum():
        raise ManifestError("primary-event symbol is invalid")
    root = evidence_root.resolve()
    raw = raw_file.resolve()
    receipt_path = receipt_file.resolve()
    if root not in raw.parents or root not in receipt_path.parents:
        raise ManifestError("primary-event files escape the evidence root")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != sha256_payload(unsigned):
        raise ManifestError("primary-event receipt hash mismatch")
    try:
        rebuilt = build_capture_receipt(
            source_uri=receipt["source_uri"],
            final_uri=receipt["final_uri"],
            published_at=receipt["published_at"],
            captured_at_start_et=receipt["captured_at_start_et"],
            captured_at_end_et=receipt["captured_at_end_et"],
            cutoff_et=receipt["cutoff_et"],
            status_code=int(receipt["status_code"]),
            content_type=receipt.get("content_type"),
            content_length=int(receipt["content_length"]),
            raw_sha256=receipt["raw_sha256"],
            publication_proof=receipt.get("publication_proof"),
        )
    except (CaptureError, KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"primary-event receipt violates the capture contract: {exc}") from exc
    if rebuilt != receipt:
        raise ManifestError("primary-event receipt differs from canonical reconstruction")
    raw_sha = sha256_file(raw)
    if raw_sha != receipt.get("raw_sha256"):
        raise ManifestError("primary-event raw bytes differ from the receipt")
    if raw.stat().st_size != int(receipt["content_length"]):
        raise ManifestError("primary-event byte length differs from the receipt")
    raw_bytes = raw.read_bytes()
    try:
        verify_publication_proof(raw=raw_bytes, receipt=receipt)
    except CaptureError as exc:
        raise ManifestError(f"primary-event publication proof is invalid: {exc}") from exc
    if _time(receipt.get("published_at")) > _time(receipt.get("cutoff_et")):
        raise ManifestError("primary event was published after cutoff")
    if _time(receipt.get("captured_at_end_et")) > _time(receipt.get("cutoff_et")):
        raise ManifestError("primary event was captured after cutoff")
    record = {
        "evidence_id": f"primary-event-{canonical.lower()}-{raw_sha[:12]}",
        "domain": "event",
        "provider": provider,
        "source_uri": str(receipt["final_uri"]),
        "raw_file_path": str(raw.relative_to(root)),
        "raw_sha256": raw_sha,
        "published_at": str(receipt["published_at"]),
        "captured_at": str(receipt["captured_at_end_et"]),
        "scope_symbols": [canonical],
        "upstream_event_id": f"issuer:{canonical}:{receipt['published_at']}:{raw_sha[:12]}",
        "consumer_domains": ["event", "relationships", "price_volume"],
    }
    if (analysis_file is None) != (analysis_receipt_file is None):
        raise ManifestError("analysis view and receipt must be supplied together")
    if analysis_file is not None and analysis_receipt_file is not None:
        analysis = analysis_file.resolve()
        analysis_receipt_path = analysis_receipt_file.resolve()
        if root not in analysis.parents or root not in analysis_receipt_path.parents:
            raise ManifestError("primary-event analysis files escape the evidence root")
        analysis_bytes = analysis.read_bytes()
        analysis_receipt = json.loads(analysis_receipt_path.read_text(encoding="utf-8"))
        try:
            verify_sec_view_receipt(
                raw=raw_bytes, analysis=analysis_bytes, receipt=analysis_receipt
            )
        except (SecViewError, KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"primary-event analysis view is invalid: {exc}") from exc
        record.update({
            "analysis_file_path": str(analysis.relative_to(root)),
            "analysis_sha256": sha256_file(analysis),
            "analysis_transform": {
                "name": analysis_receipt["transform"],
                "document_types": analysis_receipt["document_types"],
                "maximum_output_bytes": analysis_receipt["maximum_output_bytes"],
                "source_sha256": analysis_receipt["source_sha256"],
                "receipt_sha256": analysis_receipt["receipt_sha256"],
            },
        })
    return record


def merge_evidence_manifests(
    base_manifest: dict[str, Any], event_records: list[dict[str, Any]]
) -> dict[str, Any]:
    evidence = [dict(row) for row in base_manifest.get("evidence", [])]
    evidence.extend(dict(row) for row in event_records)
    ids = [str(row.get("evidence_id", "")) for row in evidence]
    if "" in ids or len(ids) != len(set(ids)):
        raise ManifestError("merged evidence IDs are blank or duplicated")
    return {"schema_version": 6, "evidence": sorted(evidence, key=lambda row: row["evidence_id"])}
