from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any, Iterable

from .hashing import sha256_payload


class SecViewError(ValueError):
    """A deterministic SEC analysis view cannot be reproduced safely."""


_DOCUMENT = re.compile(br"(?is)<DOCUMENT>.*?</DOCUMENT>")
_TYPE = re.compile(br"(?im)^<TYPE>\s*([^\r\n<]+)")
_FILENAME = re.compile(br"(?im)^<FILENAME>\s*([^\r\n<]+)")


class _VisibleText(HTMLParser):
    _SKIP = {"script", "style", "ix:hidden"}
    _BREAK = {"br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "td", "th", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP:
            self.skip_depth += 1
        elif self.skip_depth == 0 and normalized in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif self.skip_depth == 0 and normalized in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def _tag(pattern: re.Pattern[bytes], document: bytes, field: str) -> str:
    match = pattern.search(document)
    if not match:
        raise SecViewError(f"SEC document lacks {field}")
    return match.group(1).decode("utf-8", errors="strict").strip()


def build_sec_analysis_text(
    raw: bytes, *, document_types: Iterable[str], maximum_output_bytes: int
) -> bytes:
    selected = tuple(dict.fromkeys(str(value).strip().upper() for value in document_types))
    if not selected or any(not value for value in selected) or maximum_output_bytes <= 0:
        raise SecViewError("SEC view policy is invalid")
    parts = ["SEC_DOCUMENT_TEXT_VIEW_V1", f"SOURCE_SHA256={hashlib.sha256(raw).hexdigest()}"]
    found: set[str] = set()
    for document in _DOCUMENT.findall(raw):
        document_type = _tag(_TYPE, document, "TYPE").upper()
        if document_type not in selected:
            continue
        filename = _tag(_FILENAME, document, "FILENAME")
        parser = _VisibleText()
        parser.feed(document.decode("utf-8", errors="replace"))
        visible = "\n".join(
            " ".join(line.split())
            for line in "".join(parser.parts).splitlines()
            if line.strip()
        )
        if not visible:
            raise SecViewError(f"SEC document {document_type} has no visible text")
        parts.extend((f"DOCUMENT_TYPE={document_type}", f"FILENAME={filename}", visible))
        found.add(document_type)
    missing = set(selected) - found
    if missing:
        raise SecViewError(f"SEC submission lacks configured documents: {sorted(missing)}")
    output = ("\n".join(parts) + "\n").encode("utf-8")
    if len(output) > maximum_output_bytes:
        raise SecViewError("SEC text view exceeds the governed output-size limit")
    return output


def build_sec_view_receipt(
    *, raw: bytes, analysis: bytes, document_types: Iterable[str], maximum_output_bytes: int
) -> dict[str, Any]:
    canonical_types = list(dict.fromkeys(str(value).strip().upper() for value in document_types))
    rebuilt = build_sec_analysis_text(
        raw, document_types=canonical_types, maximum_output_bytes=maximum_output_bytes
    )
    if rebuilt != analysis:
        raise SecViewError("SEC analysis view is not the canonical deterministic transform")
    receipt = {
        "schema_version": 6,
        "transform": "sec_document_text_view_v1",
        "document_types": canonical_types,
        "maximum_output_bytes": maximum_output_bytes,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "analysis_sha256": hashlib.sha256(analysis).hexdigest(),
        "analysis_bytes": len(analysis),
    }
    return {**receipt, "receipt_sha256": sha256_payload(receipt)}


def verify_sec_view_receipt(*, raw: bytes, analysis: bytes, receipt: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != sha256_payload(unsigned):
        raise SecViewError("SEC analysis receipt hash mismatch")
    rebuilt = build_sec_view_receipt(
        raw=raw,
        analysis=analysis,
        document_types=receipt.get("document_types", []),
        maximum_output_bytes=int(receipt.get("maximum_output_bytes", 0)),
    )
    if rebuilt != receipt:
        raise SecViewError("SEC analysis receipt differs from canonical reconstruction")
