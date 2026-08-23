from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .hashing import sha256_file, sha256_payload
from .sec_view import SecViewError, build_sec_analysis_text


class EvidenceError(ValueError):
    """Frozen evidence is unsafe or cannot be reproduced."""


TIME_FIELDS = ("published_at", "accepted_at", "first_seen_at", "captured_at")


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{field} requires an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{field} requires an offset")
    return parsed


def _source(root: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise EvidenceError("raw_file_path is required")
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    base = root.resolve()
    if resolved != base and base not in resolved.parents:
        raise EvidenceError("raw_file_path escapes evidence root")
    if not resolved.is_file():
        raise EvidenceError("raw evidence file does not exist")
    return resolved


def _parents(record: dict[str, Any]) -> set[str]:
    parents = {str(value) for value in record.get("parent_evidence_ids", [])}
    for transform in record.get("transform_chain", []):
        if not isinstance(transform, dict):
            raise EvidenceError("transform must be an object")
        parents.update(str(value) for value in transform.get("input_ids", []))
    return parents


def _acyclic(parent_map: dict[str, set[str]]) -> None:
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            raise EvidenceError("lineage graph contains a cycle")
        if state.get(node) == 2:
            return
        state[node] = 1
        for parent in parent_map[node]:
            visit(parent)
        state[node] = 2

    for node in sorted(parent_map):
        visit(node)


def build_lineage_graph(
    records: Iterable[dict[str, Any]], evidence_root: str | Path, cutoff_et: str
) -> dict[str, Any]:
    cutoff = _time(cutoff_et, "cutoff_et")
    root = Path(evidence_root)
    items = [dict(record) for record in records]
    ids = [str(record.get("evidence_id", "")) for record in items]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise EvidenceError("evidence_id values must be present and unique")
    by_id = dict(zip(ids, items, strict=True))
    parent_map = {key: _parents(value) for key, value in by_id.items()}
    for key, parents in parent_map.items():
        missing = parents.difference(by_id)
        if missing:
            raise EvidenceError(f"orphan lineage inputs for {key}: {sorted(missing)}")
    _acyclic(parent_map)

    tokens: dict[str, set[str]] = {}
    verified: dict[str, dict[str, Any]] = {}
    for key in sorted(by_id):
        record = by_id[key]
        for field in ("domain", "provider", "source_uri", "captured_at"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise EvidenceError(f"{key} is missing {field}")
        for field in TIME_FIELDS:
            if record.get(field) is not None and _time(record[field], field) > cutoff:
                raise EvidenceError(f"{key} contains post-cutoff {field}")
        path = _source(root, record.get("raw_file_path"))
        declared = str(record.get("raw_sha256", ""))
        actual = sha256_file(path)
        if len(declared) != 64 or declared.lower() != declared or actual != declared:
            raise EvidenceError(f"{key} raw SHA-256 mismatch")
        node_tokens = {f"raw:{actual}"}
        if record.get("upstream_event_id"):
            node_tokens.add(f"event:{record['upstream_event_id']}")
        tokens[key] = node_tokens
        analysis_path = None
        if record.get("analysis_file_path") is not None or record.get("analysis_sha256") is not None:
            if not record.get("analysis_file_path") or not record.get("analysis_sha256"):
                raise EvidenceError(f"{key} has an incomplete analysis view")
            analysis_path = _source(root, record["analysis_file_path"])
            if sha256_file(analysis_path) != record["analysis_sha256"]:
                raise EvidenceError(f"{key} analysis SHA-256 mismatch")
            transform = record.get("analysis_transform")
            if not isinstance(transform, dict) or transform.get("source_sha256") != actual:
                raise EvidenceError(f"{key} analysis transform is not bound to raw evidence")
            if transform.get("name") != "sec_document_text_view_v1":
                raise EvidenceError(f"{key} analysis transform is unsupported")
            try:
                rebuilt_analysis = build_sec_analysis_text(
                    path.read_bytes(),
                    document_types=transform.get("document_types", []),
                    maximum_output_bytes=int(transform.get("maximum_output_bytes", 0)),
                )
            except (SecViewError, TypeError, ValueError) as exc:
                raise EvidenceError(f"{key} analysis transform cannot be reproduced") from exc
            if analysis_path.read_bytes() != rebuilt_analysis:
                raise EvidenceError(f"{key} analysis view differs from deterministic reconstruction")
        verified[key] = {
            **record,
            "raw_file_path": str(path),
            "raw_sha256_verified": True,
            "parent_evidence_ids": sorted(parent_map[key]),
        }
        if analysis_path is not None:
            verified[key]["analysis_file_path"] = str(analysis_path)
            verified[key]["analysis_sha256_verified"] = True

    adjacency = {key: set(parent_map[key]) for key in ids}
    owners: dict[str, str] = {}
    for key in sorted(ids):
        for token in sorted(tokens[key]):
            if token in owners:
                adjacency[key].add(owners[token])
                adjacency[owners[token]].add(key)
            else:
                owners[token] = key
        for parent in parent_map[key]:
            adjacency[parent].add(key)

    seen: set[str] = set()
    clusters: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for start in sorted(ids):
        if start in seen:
            continue
        stack, members = [start], []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            members.append(node)
            stack.extend(sorted(adjacency[node] - seen, reverse=True))
        root_tokens = sorted({token for member in members for token in tokens[member]})
        cluster_id = "lin_" + sha256_payload(root_tokens)[:20]
        for member in members:
            mapping[member] = cluster_id
            verified[member]["lineage_root_id"] = cluster_id
        clusters.append({"lineage_root_id": cluster_id, "evidence_ids": sorted(members)})

    return {
        "schema_version": 6,
        "cutoff_et": cutoff_et,
        "records": [verified[key] for key in sorted(verified)],
        "clusters": sorted(clusters, key=lambda item: item["lineage_root_id"]),
        "evidence_to_root": mapping,
        "independent_root_count": len(clusters),
    }
