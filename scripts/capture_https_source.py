#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6.capture import (  # noqa: E402
    CaptureError,
    build_capture_receipt,
    extract_sec_acceptance_proof,
    parse_timestamp,
    validate_public_https_url,
    verify_publication_proof,
)


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_public_https_url(new_url)
        if str(urlparse(new_url).hostname).lower() not in self.allowed_hosts:
            raise CaptureError("redirect host was not explicitly allowed")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze exact cutoff-safe HTTPS primary-source bytes")
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--published-at", required=True,
        help="Offset-aware timestamp, or 'auto' for a SEC complete-submission acceptance header",
    )
    parser.add_argument(
        "--publication-proof-method",
        choices=("sec_acceptance", "embedded_date_published"),
        required=True,
    )
    parser.add_argument(
        "--publication-proof-span",
        help="Exact captured source span containing the acceptance/datePublished timestamp",
    )
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument(
        "--allowed-redirect-host",
        action="append",
        default=[],
        help="Explicitly allow a trusted HTTPS redirect host; repeat when needed",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("DAILY_ORACLE_USER_AGENT", "DailyOracleV6/0.1 primary-source capture"),
    )
    args = parser.parse_args()
    if args.output.exists() or args.receipt.exists():
        raise FileExistsError("primary-source bytes and receipt are immutable")
    if args.max_bytes <= 0:
        raise ValueError("max-bytes must be positive")
    cutoff = parse_timestamp(args.cutoff_et, "cutoff_et")
    zone = ZoneInfo("America/New_York")
    started = datetime.now(zone)
    if started > cutoff:
        raise CaptureError("formal source capture started after cutoff")
    validate_public_https_url(args.url)
    initial_host = str(urlparse(args.url).hostname).lower()
    allowed_hosts = {initial_host}
    for host in args.allowed_redirect_host:
        validate_public_https_url(f"https://{host}/")
        allowed_hosts.add(host.lower().rstrip("."))
    opener = build_opener(SafeRedirectHandler(allowed_hosts))
    request = Request(args.url, headers={"User-Agent": args.user_agent, "Accept": "*/*"})
    with opener.open(request, timeout=20) as response:
        body = response.read(args.max_bytes + 1)
        if len(body) > args.max_bytes:
            raise CaptureError("primary source exceeds max-bytes")
        ended = datetime.now(zone)
        if str(urlparse(response.geturl()).hostname).lower() not in allowed_hosts:
            raise CaptureError("final response host was not explicitly allowed")
        published_at = args.published_at
        proof_span = args.publication_proof_span
        if args.publication_proof_method == "sec_acceptance" and published_at == "auto":
            published_at, extracted_span = extract_sec_acceptance_proof(body)
            if proof_span is not None and proof_span != extracted_span:
                raise CaptureError("supplied SEC proof span differs from the captured acceptance header")
            proof_span = extracted_span
        if published_at == "auto" or not proof_span:
            raise CaptureError("publication time and proof span could not be established")
        receipt = build_capture_receipt(
            source_uri=args.url,
            final_uri=response.geturl(),
            published_at=published_at,
            captured_at_start_et=started.isoformat(),
            captured_at_end_et=ended.isoformat(),
            cutoff_et=args.cutoff_et,
            status_code=int(response.status),
            content_type=response.headers.get("Content-Type"),
            content_length=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            publication_proof={
                "method": args.publication_proof_method,
                "raw_span": proof_span,
            },
        )
        verify_publication_proof(raw=body, receipt=receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "receipt": str(args.receipt),
        "raw_sha256": receipt["raw_sha256"],
        "captured_at_end_et": receipt["captured_at_end_et"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
