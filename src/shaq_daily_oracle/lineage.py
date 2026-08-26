from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .hashing import sha256_file, sha256_payload
from .collectors import CollectorError, build_capital_analysis
from .price_history import PriceHistoryError, build_price_history_analysis
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
            try:
                if transform.get("name") == "sec_document_text_view_v1":
                    rebuilt_analysis = build_sec_analysis_text(
                        path.read_bytes(),
                        document_types=transform.get("document_types", []),
                        maximum_output_bytes=int(transform.get("maximum_output_bytes", 0)),
                    )
                elif transform.get("name") == "price_path_analysis_view_v1":
                    rebuilt_analysis = build_price_history_analysis(
                        path.read_bytes(), maximum_bars=int(transform.get("maximum_bars", 0))
                    )
                elif transform.get("name") == "capital_ofi_analysis_view_v1":
                    rebuilt_analysis = build_capital_analysis(path.read_bytes())
                else:
                    raise EvidenceError(f"{key} analysis transform is unsupported")
            except (CollectorError, PriceHistoryError, SecViewError, TypeError, ValueError) as exc:
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

    # PROV semantics: a derived entity inherits every origin; it never merges its
    # parents into a new mega-root. Only identical source entities (same bytes or
    # upstream event id) share a root.
    token_parent: dict[str, str] = {}

    def token_find(token: str) -> str:
        token_parent.setdefault(token, token)
        if token_parent[token] != token:
            token_parent[token] = token_find(token_parent[token])
        return token_parent[token]

    def token_union(left: str, right: str) -> None:
        first, second = token_find(left), token_find(right)
        if first != second:
            token_parent[max(first, second)] = min(first, second)

    for key in sorted(ids):
        values = sorted(tokens[key])
        for token in values:
            token_find(token)
        for token in values[1:]:
            token_union(values[0], token)
    token_groups: dict[str, set[str]] = {}
    for token in sorted(token_parent):
        token_groups.setdefault(token_find(token), set()).add(token)
    token_to_root = {
        token: "lin_" + sha256_payload(sorted(token_groups[token_find(token)]))[:20]
        for token in token_parent
    }
    direct_roots: dict[str, set[str]] = {}
    for key in sorted(ids):
        direct_roots[key] = {token_to_root[token] for token in tokens[key]}

    ancestry: dict[str, set[str]] = {}

    def roots_for(key: str) -> set[str]:
        if key in ancestry:
            return ancestry[key]
        parents = parent_map[key]
        # A transform with declared parents is evidence derived from those inputs;
        # its serialized output is reproducibility material, not a fresh source.
        roots = (
            set().union(*(roots_for(parent) for parent in parents))
            if parents else set(direct_roots[key])
        )
        ancestry[key] = roots
        return roots

    mapping_many: dict[str, list[str]] = {}
    root_members: dict[str, set[str]] = {}
    root_components: dict[str, set[str]] = {}
    default_component = {
        "market": "market_context", "relationships": "industry_context",
        "event": "stock_event", "capital": "stock_capital",
        "derivatives": "stock_derivatives", "price_volume": "stock_price_volume",
    }
    for key in sorted(ids):
        roots = sorted(roots_for(key))
        if not roots:
            raise EvidenceError(f"{key} has no source lineage root")
        mapping_many[key] = roots
        verified[key]["lineage_root_ids"] = roots
        if len(roots) == 1:
            verified[key]["lineage_root_id"] = roots[0]
        component = str(
            verified[key].get("root_component_type")
            or default_component.get(str(verified[key].get("domain")), "unknown")
        )
        for root_id in roots:
            root_members.setdefault(root_id, set()).add(key)
            # Only origin entities define the economic role of a root. A derived
            # cross-domain view cannot relabel its ancestors.
            if not parent_map[key]:
                root_components.setdefault(root_id, set()).add(component)

    clusters = [
        {
            "lineage_root_id": root_id,
            "evidence_ids": sorted(root_members[root_id]),
            "component_types": sorted(root_components.get(root_id, {"unknown"})),
        }
        for root_id in sorted(root_members)
    ]
    compatibility_mapping = {
        key: roots[0] for key, roots in mapping_many.items() if len(roots) == 1
    }

    return {
        "schema_version": 7,
        "cutoff_et": cutoff_et,
        "records": [verified[key] for key in sorted(verified)],
        "clusters": sorted(clusters, key=lambda item: item["lineage_root_id"]),
        "evidence_to_roots": mapping_many,
        "evidence_to_root": compatibility_mapping,
        "root_component_types": {
            root_id: sorted(root_components.get(root_id, {"unknown"}))
            for root_id in sorted(root_members)
        },
        "independent_root_count": len(clusters),
    }
