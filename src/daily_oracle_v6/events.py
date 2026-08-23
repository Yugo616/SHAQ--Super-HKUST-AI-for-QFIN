from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .collectors import collection_status
from .hashing import sha256_file


class EventCaptureError(ValueError):
    """A primary event could not be captured under the cutoff contract."""


def _cik_number(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise EventCaptureError("candidate has no SEC CIK")
    return digits.zfill(10)


def _fetch(url: str, user_agent: str, maximum_bytes: int = 20 * 1024 * 1024) -> bytes:
    request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise EventCaptureError(f"SEC returned HTTP {response.status}")
        payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise EventCaptureError("SEC document exceeds the capture limit")
    return payload


def capture_sec_candidate_events(
    *, candidates: list[dict[str, Any]], universe_csv: Path, evidence_root: Path,
    output_manifest: Path, status_output: Path, cutoff_et: str, previous_close_et: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cutoff = datetime.fromisoformat(cutoff_et.replace("Z", "+00:00"))
    previous_close = datetime.fromisoformat(previous_close_et.replace("Z", "+00:00"))
    user_agent = os.environ.get("DAILY_ORACLE_SEC_USER_AGENT", "").strip()
    with universe_csv.open(newline="", encoding="utf-8-sig") as handle:
        members = {
            str(row.get("ticker", row.get("instrument", ""))).upper(): row
            for row in csv.DictReader(handle)
        }
    records = []
    statuses = []
    for candidate in candidates:
        symbol = str(candidate["symbol"]).upper()
        captured = datetime.now(ZoneInfo("America/New_York"))
        if captured > cutoff:
            raise EventCaptureError("event capture passed the evidence cutoff")
        if not user_agent:
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="provider_error",
                captured_at_et=captured.isoformat(),
                reason="DAILY_ORACLE_SEC_USER_AGENT is required by SEC access policy",
            ))
            continue
        try:
            cik = _cik_number(str(members.get(symbol, {}).get("cik_company_id", "")))
            submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            submissions_bytes = _fetch(submissions_url, user_agent)
            submissions = json.loads(submissions_bytes)
            recent = submissions.get("filings", {}).get("recent", {})
            keys = ("accessionNumber", "acceptanceDateTime", "form", "primaryDocument")
            if any(not isinstance(recent.get(key), list) for key in keys):
                raise EventCaptureError("SEC recent-filings schema is incomplete")
            events = []
            for values in zip(*(recent[key] for key in keys), strict=False):
                accession, acceptance_text, form, primary = map(str, values)
                if form not in {"8-K", "10-Q", "10-K", "6-K", "20-F"}:
                    continue
                acceptance = datetime.fromisoformat(acceptance_text.replace("Z", "+00:00"))
                if acceptance.tzinfo is None:
                    acceptance = acceptance.replace(tzinfo=ZoneInfo("America/New_York"))
                if previous_close < acceptance <= cutoff:
                    events.append((acceptance, accession, form, primary))
            if not events:
                statuses.append(collection_status(
                    domain="event", symbol=symbol, status="no_data",
                    captured_at_et=captured.isoformat(), reason="no qualifying SEC filing in the event window",
                ))
                continue
            acceptance, accession, form, primary = max(events, key=lambda row: row[0])
            accession_compact = accession.replace("-", "")
            cik_compact = str(int(cik))
            source_uri = f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/{accession_compact}/{primary}"
            raw = _fetch(source_uri, user_agent)
            completed = datetime.now(ZoneInfo("America/New_York"))
            if completed > cutoff:
                raise EventCaptureError("primary filing completed after cutoff")
            path = evidence_root / "events_by_symbol" / f"{symbol}-{accession_compact}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise FileExistsError("primary event artifact is immutable")
            path.write_bytes(raw)
            record = {
                "evidence_id": f"sec-event-{symbol.lower()}-{accession_compact}",
                "domain": "event",
                "provider": "SEC EDGAR",
                "source_uri": source_uri,
                "raw_file_path": str(path.resolve().relative_to(evidence_root.resolve())),
                "raw_sha256": sha256_file(path),
                "published_at": acceptance.isoformat(),
                "accepted_at": acceptance.isoformat(),
                "captured_at": completed.isoformat(),
                "scope_symbols": [symbol],
                "upstream_event_id": f"sec:{accession}",
                "form": form,
            }
            records.append(record)
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="collected",
                captured_at_et=completed.isoformat(), record_count=1,
            ))
        except Exception as exc:
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="provider_error",
                captured_at_et=datetime.now(ZoneInfo("America/New_York")).isoformat(),
                reason=f"{type(exc).__name__}: {exc}",
            ))
    manifest = {"schema_version": 6, "evidence": sorted(records, key=lambda row: row["evidence_id"])}
    status_document = {"schema_version": 6, "statuses": sorted(statuses, key=lambda row: row["symbol"])}
    for path, value in ((output_manifest, manifest), (status_output, status_document)):
        if path.exists():
            raise FileExistsError("event capture output is immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, status_document
