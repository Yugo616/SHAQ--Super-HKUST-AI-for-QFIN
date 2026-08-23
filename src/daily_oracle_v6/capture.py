from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .hashing import sha256_payload


class CaptureError(ValueError):
    """A primary source cannot be frozen safely before the cutoff."""


def parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CaptureError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CaptureError(f"{field} requires an explicit offset")
    return parsed


def validate_public_https_url(url: str, *, resolve: bool = False) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise CaptureError("source URL must be credential-free public HTTPS on port 443")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise CaptureError("source URL cannot use a non-public IP address")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise CaptureError("source URL cannot use a local hostname")
    return url


def build_capture_receipt(
    *,
    source_uri: str,
    final_uri: str,
    published_at: str,
    captured_at_start_et: str,
    captured_at_end_et: str,
    cutoff_et: str,
    status_code: int,
    content_type: str | None,
    content_length: int,
    raw_sha256: str,
    publication_proof: dict[str, str] | None = None,
) -> dict[str, Any]:
    cutoff = parse_timestamp(cutoff_et, "cutoff_et")
    published = parse_timestamp(published_at, "published_at")
    started = parse_timestamp(captured_at_start_et, "captured_at_start_et")
    ended = parse_timestamp(captured_at_end_et, "captured_at_end_et")
    if published > ended or published > cutoff or started > ended or ended > cutoff:
        raise CaptureError("publication or capture falls outside the formal cutoff")
    if status_code != 200 or content_length <= 0:
        raise CaptureError("primary-source response is empty or unsuccessful")
    if len(raw_sha256) != 64 or raw_sha256.lower() != raw_sha256:
        raise CaptureError("raw SHA-256 is invalid")
    receipt = {
        "schema_version": 6,
        "source_uri": validate_public_https_url(source_uri, resolve=False),
        "final_uri": validate_public_https_url(final_uri, resolve=False),
        "published_at": published_at,
        "captured_at_start_et": captured_at_start_et,
        "captured_at_end_et": captured_at_end_et,
        "cutoff_et": cutoff_et,
        "status_code": status_code,
        "content_type": content_type,
        "content_length": content_length,
        "raw_sha256": raw_sha256,
    }
    if publication_proof is not None:
        if set(publication_proof) != {"method", "raw_span"}:
            raise CaptureError("publication proof differs from the exact contract")
        if publication_proof["method"] not in {"sec_acceptance", "embedded_date_published"}:
            raise CaptureError("publication proof method is not admissible")
        if not isinstance(publication_proof["raw_span"], str) or not publication_proof["raw_span"]:
            raise CaptureError("publication proof requires a non-empty raw span")
        receipt["publication_proof"] = dict(publication_proof)
    receipt["receipt_sha256"] = sha256_payload(receipt)
    return receipt


def verify_publication_proof(*, raw: bytes, receipt: dict[str, Any]) -> dict[str, str]:
    proof = receipt.get("publication_proof")
    if not isinstance(proof, dict) or set(proof) != {"method", "raw_span"}:
        raise CaptureError("primary event requires an exact publication proof")
    span = proof.get("raw_span")
    if not isinstance(span, str) or not span or span.encode("utf-8") not in raw:
        raise CaptureError("publication proof span is absent from the captured bytes")
    method = proof.get("method")
    if method == "sec_acceptance":
        compact = re.search(
            r"ACCEPTANCE-DATETIME(?:\s*:\s*|>\s*)(\d{14})", span, re.IGNORECASE
        )
        displayed = re.search(
            r"Accepted[\s\S]{0,200}?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})",
            span,
            re.IGNORECASE,
        )
        if compact:
            proven = datetime.strptime(compact.group(1), "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        elif displayed:
            proven = datetime.fromisoformat(f"{displayed.group(1)}T{displayed.group(2)}").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        else:
            raise CaptureError("SEC proof span lacks an acceptance timestamp")
    elif method == "embedded_date_published":
        match = re.search(
            r"datePublished[^0-9]{0,40}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))",
            span,
            re.IGNORECASE,
        )
        if not match:
            raise CaptureError("embedded proof span lacks an offset-aware datePublished value")
        proven = parse_timestamp(match.group(1), "publication_proof.raw_span")
    else:
        raise CaptureError("publication proof method is not admissible")
    declared = parse_timestamp(str(receipt.get("published_at", "")), "published_at")
    if proven != declared:
        raise CaptureError("declared publication time differs from the captured proof")
    return {"method": str(method), "raw_span": span}


def extract_sec_acceptance_proof(raw: bytes) -> tuple[str, str]:
    match = re.search(
        rb"ACCEPTANCE-DATETIME(?:\s*:\s*|>\s*)(\d{14})", raw, re.IGNORECASE
    )
    if not match:
        raise CaptureError("SEC submission lacks an acceptance header")
    span = match.group(0).decode("ascii")
    published = datetime.strptime(match.group(1).decode("ascii"), "%Y%m%d%H%M%S").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    return published.isoformat(), span
