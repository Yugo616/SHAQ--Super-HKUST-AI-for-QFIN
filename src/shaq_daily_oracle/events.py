from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .collectors import collection_status
from .hashing import sha256_file
from .sec_view import build_sec_analysis_text


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


def _fetch_with_retry(
    url: str, user_agent: str, *, maximum_bytes: int,
    attempts: int, backoff_seconds: float,
) -> bytes:
    if attempts <= 0 or backoff_seconds < 0:
        raise EventCaptureError("SEC retry policy is invalid")
    for attempt in range(attempts):
        try:
            return _fetch(url, user_agent, maximum_bytes=maximum_bytes)
        except (URLError, TimeoutError, ConnectionError):
            if attempt + 1 == attempts:
                raise
            time.sleep(backoff_seconds * (attempt + 1))
    raise EventCaptureError("SEC retry policy exhausted without a result")


def _sec_identity() -> str:
    identity = os.environ.get("DAILY_ORACLE_SEC_USER_AGENT", "").strip()
    if not identity:
        raise EventCaptureError("DAILY_ORACLE_SEC_USER_AGENT is required by SEC access policy")
    return identity


def capture_sec_universe_events(
    *, universe_csv: Path, evidence_root: Path, output_manifest: Path,
    status_output: Path, cutoff_et: str, previous_close_et: str,
    config: dict[str, Any], analysis_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Discover current SEC filings before candidate selection using latest-filings feeds."""

    cutoff = datetime.fromisoformat(cutoff_et.replace("Z", "+00:00"))
    previous_close = datetime.fromisoformat(previous_close_et.replace("Z", "+00:00"))
    identity = _sec_identity()
    with universe_csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    by_cik = {
        _cik_number(str(row.get("cik_company_id", ""))): str(
            row.get("ticker", row.get("instrument", ""))
        ).upper()
        for row in rows if str(row.get("cik_company_id", "")).strip()
    }
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    discovered: dict[str, tuple[datetime, str, str, str]] = {}
    statuses = []
    bindings = config.get("parameter_bindings", {})
    required = (
        "sec_forms", "sec_feed_page_size", "sec_feed_max_pages_per_form",
        "sec_feed_maximum_bytes", "sec_document_maximum_bytes",
        "sec_retry_attempts", "sec_retry_backoff_seconds",
    )
    if any(len(bindings.get(name, [])) != 3 for name in required):
        raise EventCaptureError("event-discovery config is not fully governed")
    for form in config["sec_forms"]:
        for page in range(int(config["sec_feed_max_pages_per_form"])):
            start = page * int(config["sec_feed_page_size"])
            url = (
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=include"
                f"&type={form}&count={int(config['sec_feed_page_size'])}&start={start}&output=atom"
            )
            try:
                root = ElementTree.fromstring(_fetch_with_retry(
                    url, identity, maximum_bytes=int(config["sec_feed_maximum_bytes"]),
                    attempts=int(config["sec_retry_attempts"]),
                    backoff_seconds=float(config["sec_retry_backoff_seconds"]),
                ))
                oldest = cutoff
                entries = root.findall("atom:entry", namespace)
                if not entries:
                    break
                for entry in entries:
                    updated_text = entry.findtext("atom:updated", default="", namespaces=namespace)
                    link_node = entry.find("atom:link", namespace)
                    href = str(link_node.attrib.get("href", "")) if link_node is not None else ""
                    match = re.search(
                        r"/Archives/edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})-index\.html", href
                    )
                    if not match or not updated_text:
                        continue
                    accepted = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
                    oldest = min(oldest, accepted)
                    if not previous_close < accepted <= cutoff:
                        continue
                    cik = match.group(1).zfill(10)
                    symbol = by_cik.get(cik)
                    if not symbol:
                        continue
                    accession = match.group(2)
                    accession_compact = accession.replace("-", "")
                    source = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_compact}/{accession}.txt"
                    previous = discovered.get(symbol)
                    if previous is None or accepted > previous[0]:
                        discovered[symbol] = (accepted, accession_compact, form, source)
                if oldest <= previous_close:
                    break
            except Exception as exc:
                statuses.append(collection_status(
                    domain="event", symbol="*", status="provider_error",
                    captured_at_et=datetime.now(ZoneInfo("America/New_York")).isoformat(),
                    reason=f"SEC latest-filings {form}: {type(exc).__name__}: {exc}",
                ))
                break
    records = []
    for symbol, (accepted, accession_compact, form, source) in sorted(discovered.items()):
        try:
            raw = _fetch_with_retry(
                source, identity, maximum_bytes=int(config["sec_document_maximum_bytes"]),
                attempts=int(config["sec_retry_attempts"]),
                backoff_seconds=float(config["sec_retry_backoff_seconds"]),
            )
            captured = datetime.now(ZoneInfo("America/New_York"))
            if captured > cutoff:
                raise EventCaptureError("primary filing completed after cutoff")
            path = evidence_root / "events_by_symbol" / f"{symbol}-{accession_compact}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise FileExistsError("primary event artifact is immutable")
            analysis_bytes = build_sec_analysis_text(
                raw, document_types=analysis_config["document_types"],
                maximum_output_bytes=int(analysis_config["maximum_output_bytes"]),
            )
            analysis_path = path.with_suffix(".analysis.txt")
            if analysis_path.exists():
                raise FileExistsError("primary event analysis artifact is immutable")
            path.write_bytes(raw)
            analysis_path.write_bytes(analysis_bytes)
            records.append({
                "evidence_id": f"sec-event-{symbol.lower()}-{accession_compact}",
                "domain": "event", "root_component_type": "stock_event",
                "provider": "SEC EDGAR", "source_uri": source,
                "raw_file_path": str(path.resolve().relative_to(evidence_root.resolve())),
                "raw_sha256": sha256_file(path), "published_at": accepted.isoformat(),
                "accepted_at": accepted.isoformat(), "captured_at": captured.isoformat(),
                "scope_symbols": [symbol], "upstream_event_id": f"sec:{accession_compact}",
                "form": form,
                "consumer_domains": ["event", "relationships", "price_volume"],
                "analysis_file_path": str(analysis_path.resolve().relative_to(evidence_root.resolve())),
                "analysis_sha256": sha256_file(analysis_path),
                "analysis_transform": {
                    "name": "sec_document_text_view_v1",
                    "document_types": analysis_config["document_types"],
                    "maximum_output_bytes": int(analysis_config["maximum_output_bytes"]),
                    "source_sha256": sha256_file(path),
                },
            })
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="collected",
                captured_at_et=captured.isoformat(), record_count=1,
            ))
        except Exception as exc:
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="provider_error",
                captured_at_et=datetime.now(ZoneInfo("America/New_York")).isoformat(),
                reason=f"{type(exc).__name__}: {exc}",
            ))
    manifest = {"schema_version": 7, "evidence": sorted(records, key=lambda row: row["evidence_id"])}
    status = {"schema_version": 7, "statuses": sorted(statuses, key=lambda row: (row["symbol"], row.get("reason") or ""))}
    for path, value in ((output_manifest, manifest), (status_output, status)):
        if path.exists():
            raise FileExistsError("event discovery output is immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, status


def capture_futu_earnings_calendar(
    *, universe_csv: Path, evidence_root: Path, output_manifest: Path,
    status_output: Path, trade_date: str, cutoff_et: str, host: str, port: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture per-symbol calendar/expectation rows; these never replace issuer facts."""

    cutoff = datetime.fromisoformat(cutoff_et.replace("Z", "+00:00"))
    if datetime.now(ZoneInfo("America/New_York")) > cutoff:
        raise EventCaptureError("earnings calendar capture passed the evidence cutoff")
    with universe_csv.open(newline="", encoding="utf-8-sig") as handle:
        members = {
            str(row.get("ticker", row.get("instrument", ""))).upper()
            for row in csv.DictReader(handle)
        }
    from futu import Market, OpenQuoteContext, RET_OK  # type: ignore

    quote = OpenQuoteContext(host=host, port=port)
    try:
        ret, frame = quote.get_earnings_calendar(
            Market.US, begin_date=trade_date, end_date=trade_date
        )
        if ret != RET_OK:
            raise EventCaptureError(str(frame))
        records, statuses = [], []
        for _, row in frame.iterrows():
            code = str(row.get("code", row.get("security", ""))).upper()
            symbol = code.removeprefix("US.")
            if symbol not in members:
                continue
            captured = datetime.now(ZoneInfo("America/New_York"))
            fields = (
                "security", "code", "name", "earnings_date", "earnings_timestamp",
                "pub_type", "eps_actual", "eps_predict", "revenue_actual",
                "revenue_predict", "ebit_actual", "ebit_predict",
            )
            document = {
                "schema_version": 7, "domain": "event", "symbol": symbol,
                "captured_at_et": captured.isoformat(),
                "role": "calendar_and_pre_event_expectations_only",
                "issuer_fact_source_required": True,
                "fields": {key: (None if str(row.get(key, "")) in {"nan", "None"} else row.get(key)) for key in fields},
            }
            path = evidence_root / "earnings_calendar_by_symbol" / f"{symbol}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise FileExistsError("earnings calendar artifact is immutable")
            path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
            records.append({
                "evidence_id": f"futu-earnings-calendar-{symbol.lower()}",
                "domain": "event", "root_component_type": "stock_event",
                "provider": "Futu OpenD", "source_uri": f"futu-opend://earnings-calendar/US.{symbol}",
                "raw_file_path": str(path.resolve().relative_to(evidence_root.resolve())),
                "raw_sha256": sha256_file(path), "captured_at": captured.isoformat(),
                "scope_symbols": [symbol],
                "consumer_domains": ["event", "relationships"],
            })
            statuses.append(collection_status(
                domain="event", symbol=symbol, status="collected",
                captured_at_et=captured.isoformat(), record_count=1,
                reason="Futu calendar/expectations supplement; issuer facts still require SEC or IR",
            ))
    finally:
        quote.close()
    manifest = {"schema_version": 7, "evidence": sorted(records, key=lambda row: row["evidence_id"])}
    status = {"schema_version": 7, "statuses": sorted(statuses, key=lambda row: row["symbol"])}
    for path, value in ((output_manifest, manifest), (status_output, status)):
        if path.exists():
            raise FileExistsError("earnings calendar output is immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, status


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
