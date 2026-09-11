from __future__ import annotations

import html
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .hashing import sha256_payload
from .research_batch import ResearchBatchError, load_frozen_evidence


class ResearchDashboardError(ValueError):
    """The disposable research index does not match its immutable source files."""


def _read(path: Path, fallback: Any = None) -> Any:
    if not path.is_file():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_variant(path: Path) -> dict[str, Any]:
    value = _read(path, {})
    if not isinstance(value, dict) or not value:
        raise ResearchDashboardError("variant result is missing")
    declared = value.get("variant_result_sha256")
    unsigned = {key: item for key, item in value.items() if key != "variant_result_sha256"}
    if declared != sha256_payload(unsigned):
        raise ResearchDashboardError("variant result hash mismatch")
    return value


def _verified_labels(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"labels": {}}
    value = _read(path, {})
    declared = value.get("labels_sha256") if isinstance(value, dict) else None
    unsigned = {key: item for key, item in value.items() if key != "labels_sha256"}
    if not declared or declared != sha256_payload(unsigned):
        raise ResearchDashboardError("research labels hash mismatch")
    return value


def _verified_skill_snapshot(path: Path) -> dict[str, Any]:
    value = _read(path, {})
    declared = value.get("skill_snapshot_sha256") if isinstance(value, dict) else None
    unsigned = {
        key: item for key, item in value.items()
        if key != "skill_snapshot_sha256"
    } if isinstance(value, dict) else {}
    if not declared or declared != sha256_payload(unsigned):
        raise ResearchDashboardError("Skill snapshot hash mismatch")
    documents = value.get("documents")
    hashes = value.get("document_sha256")
    if not isinstance(documents, dict) or not isinstance(hashes, dict) or set(documents) != set(hashes):
        raise ResearchDashboardError("Skill snapshot document list is invalid")
    for name, content in documents.items():
        if not isinstance(content, str) or hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest() != hashes[name]:
            raise ResearchDashboardError("Skill snapshot document hash mismatch")
    return value


def _verified_model_call(path: Path) -> dict[str, Any]:
    value = _read(path, {})
    if not isinstance(value, dict) or value.get("cache_key") != path.stem:
        raise ResearchDashboardError("model call identity mismatch")
    declared = value.get("cache_document_sha256")
    unsigned = {
        key: item for key, item in value.items()
        if key != "cache_document_sha256"
    }
    if not declared or declared != sha256_payload(unsigned):
        raise ResearchDashboardError("model call document hash mismatch")
    if not isinstance(value.get("prompt"), str) or not isinstance(value.get("schema"), dict):
        raise ResearchDashboardError("model call exact input is missing")
    key_document = value.get("key_document", {})
    if set(key_document) != {
        "cache_schema_version", "profile_sha256", "prompt_sha256", "schema_sha256"
    } or key_document.get("cache_schema_version") != 2:
        raise ResearchDashboardError("model call content key is unsupported")
    if key_document.get("prompt_sha256") != hashlib.sha256(
        value["prompt"].encode("utf-8")
    ).hexdigest():
        raise ResearchDashboardError("model call prompt hash mismatch")
    if key_document.get("schema_sha256") != sha256_payload(value["schema"]):
        raise ResearchDashboardError("model call schema hash mismatch")
    if sha256_payload(key_document) != value["cache_key"]:
        raise ResearchDashboardError("model call content key mismatch")
    if sha256_payload(value.get("result")) != value.get("result_sha256"):
        raise ResearchDashboardError("model call result hash mismatch")
    if sha256_payload(value.get("audit")) != value.get("audit_sha256"):
        raise ResearchDashboardError("model call audit hash mismatch")
    audit = value.get("audit", {})
    if (
        audit.get("profile_sha256") != key_document.get("profile_sha256")
        or audit.get("prompt_sha256") != key_document.get("prompt_sha256")
        or audit.get("schema_sha256") != key_document.get("schema_sha256")
        or audit.get("output_sha256") != value.get("result_sha256")
    ):
        raise ResearchDashboardError("model call audit differs from its frozen payload")
    return value


def _verified_batch_manifest(value: dict[str, Any], batch_id: str) -> dict[str, Any]:
    if value.get("schema_version") != 2 or value.get("batch_id") != batch_id:
        raise ResearchDashboardError("batch manifest is missing or unsupported")
    identity = value.get("batch_identity")
    if not isinstance(identity, dict) or value.get("batch_identity_sha256") != sha256_payload(identity):
        raise ResearchDashboardError("batch identity hash mismatch")
    if value.get("skill_snapshot_sha256s") != identity.get("skill_snapshot_sha256s"):
        raise ResearchDashboardError("batch Skill identity mismatch")
    return value


def _verified_batch_status(value: dict[str, Any], batch_id: str) -> dict[str, Any]:
    if not value:
        return {}
    if value.get("schema_version") != 2 or value.get("batch_id") != batch_id:
        raise ResearchDashboardError("batch status identity mismatch")
    declared = value.get("batch_status_sha256")
    unsigned = {
        key: item for key, item in value.items()
        if key != "batch_status_sha256"
    }
    if not declared or declared != sha256_payload(unsigned):
        raise ResearchDashboardError("batch status hash mismatch")
    return value


class ResearchDashboardIndex:
    """SQLite is only a view; immutable batch files remain the source of truth."""

    def __init__(self, *, batches_root: Path, database: Path) -> None:
        self.batches_root = batches_root.resolve()
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_batches (
              batch_id TEXT PRIMARY KEY,
              trade_date TEXT NOT NULL,
              cutoff_status TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              candidate_count INTEGER NOT NULL,
              completed_variant_count INTEGER NOT NULL,
              failed_variant_count INTEGER NOT NULL,
              source_valid INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_variants (
              batch_id TEXT NOT NULL,
              variant_key TEXT NOT NULL,
              label TEXT NOT NULL,
              result_sha256 TEXT NOT NULL,
              prediction_count INTEGER NOT NULL,
              PRIMARY KEY (batch_id, variant_key)
            );
            CREATE TABLE IF NOT EXISTS research_predictions (
              batch_id TEXT NOT NULL,
              variant_key TEXT NOT NULL,
              symbol TEXT NOT NULL,
              direction TEXT NOT NULL,
              actual_direction TEXT,
              correct INTEGER,
              PRIMARY KEY (batch_id, variant_key, symbol)
            );
            """
        )
        return connection

    def rebuild(self) -> None:
        self.batches_root.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM research_predictions")
            connection.execute("DELETE FROM research_variants")
            connection.execute("DELETE FROM research_batches")
            for root in sorted(self.batches_root.glob("LAB-*")):
                manifest = _read(root / "batch_manifest.json", {})
                status = _read(root / "batch_status.json", {})
                if not manifest or manifest.get("batch_id") != root.name:
                    continue
                evidence_hash = str(manifest.get("batch_identity", {}).get("evidence_hash", ""))
                source_valid = False
                candidate_count = 0
                cutoff_status = "unknown"
                try:
                    detail = self.batch_detail(root.name)
                    source_valid = True
                    candidate_count = len(detail["evidence"].get("candidates", []))
                    cutoff_status = str(detail["evidence"].get("cutoff_status", "unknown"))
                except (ResearchDashboardError, ResearchBatchError, OSError, json.JSONDecodeError):
                    source_valid = False
                completed = status.get("completed_variants", [])
                failed = status.get("failed_variants", {})
                connection.execute(
                    "INSERT INTO research_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        root.name, root.name[4:14], cutoff_status, evidence_hash,
                        candidate_count, len(completed), len(failed), 1 if source_valid else 0,
                    ),
                )
                if not source_valid:
                    continue
                try:
                    labels = _verified_labels(root / "labels.json").get("labels", {})
                except ResearchDashboardError:
                    labels = {}
                for path in sorted((root / "variants").glob("*/variant_result.json")):
                    try:
                        result = _verified_variant(path)
                    except ResearchDashboardError:
                        continue
                    variant = result["variant"]
                    key = f"{variant['author']}/{variant['version_id']}"
                    connection.execute(
                        "INSERT INTO research_variants VALUES (?, ?, ?, ?, ?)",
                        (
                            root.name, key, variant["label"], result["variant_result_sha256"],
                            len(result.get("predictions", [])),
                        ),
                    )
                    for prediction in result.get("predictions", []):
                        symbol = prediction["symbol"]
                        label = labels.get(symbol, {})
                        actual = label.get("actual_direction") if label.get("status") in {
                            "provisional", "final"
                        } else None
                        correct = None
                        if actual in {"bullish", "bearish", "neutral"}:
                            correct = 1 if actual == prediction["direction"] else 0
                        connection.execute(
                            "INSERT INTO research_predictions VALUES (?, ?, ?, ?, ?, ?)",
                            (root.name, key, symbol, prediction["direction"], actual, correct),
                        )
            connection.commit()

    def _find_evidence(self, evidence_hash: str) -> Path | None:
        if not evidence_hash:
            return None
        evidence_root = self.batches_root.parent / "evidence" / evidence_hash
        if evidence_root.is_dir():
            return evidence_root
        for path in self.batches_root.parent.glob("evidence-*/*"):
            manifest = _read(path / "evidence_manifest.json", {})
            if manifest.get("evidence_hash") == evidence_hash:
                return path
        return None

    def overview(self) -> dict[str, Any]:
        self.rebuild()
        with closing(self._connect()) as connection:
            batches = [dict(row) for row in connection.execute(
                "SELECT * FROM research_batches ORDER BY trade_date DESC, batch_id DESC"
            )]
            versions = [dict(row) for row in connection.execute(
                "SELECT variant_key, label, COUNT(DISTINCT batch_id) AS batches, "
                "COUNT(*) FILTER (WHERE prediction_count > 0) AS nonempty_batches, "
                "SUM(prediction_count) AS predictions FROM research_variants "
                "GROUP BY variant_key, label ORDER BY label"
            )]
            performance = [dict(row) for row in connection.execute(
                "SELECT variant_key, COUNT(correct) AS evaluated, "
                "COALESCE(SUM(correct), 0) AS correct FROM research_predictions "
                "GROUP BY variant_key ORDER BY variant_key"
            )]
        latest = None
        for row in batches:
            if not row["source_valid"]:
                continue
            try:
                latest = self.batch_detail(row["batch_id"])
                break
            except (ResearchDashboardError, ResearchBatchError):
                continue
        daily_results = self._daily_results(batches)
        from .virtual_accounts import AccountStore
        accounts = AccountStore(self.batches_root.parent / 'virtual_accounts').view(
            self.account_rows(daily_results))
        return {
            "generated_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "batches": batches, "versions": versions, "performance": performance,
            "latest": latest, "daily_results": daily_results, "virtual_accounts": accounts,
        }

    def account_rows(self, daily_results):
        """Join minute observations only at the execution/account boundary."""
        from .minute_settlements import MINUTE_NAMESPACE, MinuteStore
        store = MinuteStore(self.batches_root.parent / MINUTE_NAMESPACE)
        return [dict(row, minute=store.snapshot(row['trade_date'],
                    [p['symbol'] for p in row['predictions']]))
                for row in daily_results if row.get('series_key')]

    def _daily_results(self, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the simple, comparable research replay view from immutable files.

        SQLite remains only an index.  This view deliberately uses official
        unadjusted open/close labels and one notional share; it never implies a
        broker fill or feeds any result back into a premarket decision.
        """
        chronological = sorted(batches, key=lambda row: (str(row["trade_date"]), str(row["batch_id"])))
        details, first = {}, {}
        for batch in chronological:
            if not batch.get('source_valid'):
                continue
            try:
                detail = self.batch_detail(batch['batch_id'])
            except (ResearchDashboardError, ResearchBatchError):
                continue
            details[batch['batch_id']] = detail
            for key, variant in detail.get('variants', {}).items():
                if variant.get('score_eligible') is not True or detail['evidence']['cutoff_status'] != 'on_time':
                    continue
                completed = variant.get('completed_at_et')
                if not completed:
                    continue
                identity = (batch['trade_date'], key, variant['variant'].get('version_sha256'), variant.get('model_profile_sha256'))
                candidate = (datetime.fromisoformat(completed), batch['batch_id'])
                if identity not in first or candidate < first[identity]:
                    first[identity] = candidate
        cumulative: dict[str, float] = {}
        scored_sessions: set[tuple[str, str]] = set()
        output: list[dict[str, Any]] = []
        for batch in chronological:
            if not batch.get("source_valid"):
                output.append({
                    "batch_id": batch["batch_id"], "trade_date": batch["trade_date"],
                    "variant_key": "engineering-failure", "label": "工程故障",
                    "predictions": [], "correct": 0, "incorrect": 0,
                    "daily_pnl": None, "cumulative_pnl": None,
                    "status": "engineering_failure", "cutoff_status": batch["cutoff_status"],
                })
                continue
            try:
                detail = details[str(batch["batch_id"])]
            except KeyError:
                continue
            labels = detail.get("labels", {}).get("labels", {})
            for key, variant in sorted(detail.get("variants", {}).items()):
                model_hash = str(variant.get("model_profile_sha256", ""))
                series_key = key + ":" + str(variant.get("variant", {}).get("version_sha256", "")) + ":" + model_hash
                session_key = (str(batch["trade_date"]), series_key)
                source_eligible = variant.get('score_eligible') is True and detail['evidence'].get('cutoff_status') == 'on_time'
                identity = (batch['trade_date'], key, variant['variant'].get('version_sha256'), variant.get('model_profile_sha256'))
                eligible = source_eligible and identity in first and first[identity][1] == batch['batch_id'] and session_key not in scored_sessions
                if eligible:
                    scored_sessions.add(session_key)
                predictions = list(variant.get("predictions", []))
                available = []
                pending = False
                statuses = set()
                for prediction in predictions:
                    label = labels.get(str(prediction.get("symbol", "")), {})
                    if label.get("status") not in {"provisional", "final"}:
                        pending = True
                        continue
                    if not isinstance(label.get("official_unadjusted_open"), (int, float)) or not isinstance(label.get("official_unadjusted_close"), (int, float)):
                        pending = True
                        continue
                    available.append((prediction, label))
                    statuses.add(label.get("status"))
                if not predictions:
                    state = "empty"
                    correct = incorrect = 0
                    daily_pnl: float | None = 0.0
                elif pending or len(available) != len(predictions):
                    state = "pending"
                    correct = incorrect = 0
                    daily_pnl = None
                else:
                    state = "provisional" if "provisional" in statuses else "final"
                    correct = sum(
                        1 for prediction, label in available
                        if prediction.get("direction") == label.get("actual_direction")
                    )
                    incorrect = len(available) - correct  # a flat outcome is intentionally incorrect
                    daily_pnl = round(sum(
                        float(label["official_unadjusted_close"]) - float(label["official_unadjusted_open"])
                        if prediction.get("direction") == "bullish"
                        else float(label["official_unadjusted_open"]) - float(label["official_unadjusted_close"])
                        for prediction, label in available
                    ), 6)
                previous = cumulative.get(series_key, 0.0)
                if daily_pnl is not None and eligible:
                    cumulative[series_key] = round(previous + daily_pnl, 6)
                output.append({
                    "batch_id": batch["batch_id"], "trade_date": batch["trade_date"],
                    "variant_key": key, "label": variant.get("variant", {}).get("label", key),
                    "series_key": series_key, "score_eligible": eligible,
                    "method_identity": variant.get('variant', {}).get('version_sha256'),
                    "model_identity": variant.get('model_profile_sha256'),
                    "source_eligible": source_eligible,
                    "completed_at_et": variant.get('completed_at_et'),
                    "publication_deadline_et": datetime.fromisoformat(batch['trade_date']).replace(hour=9, tzinfo=ZoneInfo('America/New_York')).isoformat(),
                    "variant_result_sha256": variant.get('variant_result_sha256'),
                    "labels": {p['symbol']: labels.get(p['symbol'], {}) for p in predictions},
                    "model": variant.get("model_name") or next((x.get("response_model") for x in detail.get("model_calls", []) if x.get("response_model")), "未记录模型"),
                    "predictions": [{"symbol": row.get("symbol"), "direction": row.get("direction")} for row in predictions],
                    "correct": correct, "incorrect": incorrect,
                    "daily_pnl": daily_pnl,
                    "cumulative_pnl": cumulative.get(series_key, previous),
                    "status": state, "cutoff_status": detail["evidence"].get("cutoff_status", "unknown"),
                })
            for key, reason in sorted((detail.get("status", {}).get("failed_variants", {}) or {}).items()):
                output.append({
                    "batch_id": batch["batch_id"], "trade_date": batch["trade_date"],
                    "variant_key": key, "label": key, "predictions": [], "correct": 0, "incorrect": 0,
                    "daily_pnl": None, "cumulative_pnl": cumulative.get(key, 0.0),
                    "status": "engineering_failure", "failure_reason": str(reason),
                    "cutoff_status": detail["evidence"].get("cutoff_status", "unknown"),
                })
        return sorted(output, key=lambda row: (str(row["trade_date"]), str(row["variant_key"])), reverse=True)

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        if Path(batch_id).name != batch_id:
            raise ResearchDashboardError("invalid batch id")
        root = (self.batches_root / batch_id).resolve()
        try:
            root.relative_to(self.batches_root)
        except ValueError as exc:
            raise ResearchDashboardError("batch path escapes research storage") from exc
        manifest = _verified_batch_manifest(
            _read(root / "batch_manifest.json", {}), batch_id
        )
        status = _verified_batch_status(
            _read(root / "batch_status.json", {}), batch_id
        )
        evidence_hash = manifest["batch_identity"]["evidence_hash"]
        evidence_root = self._find_evidence(evidence_hash)
        if evidence_root is None:
            raise ResearchDashboardError("batch evidence is unavailable")
        evidence = load_frozen_evidence(evidence_root)
        skill_snapshots = {}
        for path in sorted((root / "skills").glob("*.json")):
            snapshot = _verified_skill_snapshot(path)
            variant = snapshot.get("variant", {})
            key = f"{variant.get('author')}/{variant.get('version_id')}"
            expected = manifest.get("skill_snapshot_sha256s", {}).get(key)
            if not expected or expected != snapshot["skill_snapshot_sha256"]:
                raise ResearchDashboardError("batch Skill snapshot differs from its manifest")
            skill_snapshots[key] = {
                "skill_snapshot_sha256": snapshot["skill_snapshot_sha256"],
                "document_sha256": snapshot["document_sha256"],
                "documents": snapshot["documents"],
            }
        if set(skill_snapshots) != set(manifest.get("skill_snapshot_sha256s", {})):
            raise ResearchDashboardError("batch Skill snapshot set is incomplete")
        model_calls = []
        for path in sorted((root / "model_calls").glob("*.json")):
            call = _verified_model_call(path)
            audit = call["audit"]
            model_calls.append({
                "cache_key": call["cache_key"],
                "cache_document_sha256": call["cache_document_sha256"],
                "profile_sha256": call["key_document"].get("profile_sha256"),
                "prompt_sha256": call["key_document"].get("prompt_sha256"),
                "schema_sha256": call["key_document"].get("schema_sha256"),
                "response_model": audit.get("response_model"),
                "provider": audit.get("provider"),
                "started_at_et": audit.get("started_at_et"),
                "completed_at_et": audit.get("completed_at_et"),
            })
        variants = {}
        expected_model_calls: dict[str, str] = {}
        for path in sorted((root / "variants").glob("*/variant_result.json")):
            result = _verified_variant(path)
            key = f"{result['variant']['author']}/{result['variant']['version_id']}"
            if result.get("evidence_hash") != evidence_hash:
                raise ResearchDashboardError("variant evidence identity mismatch")
            if result.get("model_profile_sha256") != manifest["batch_identity"].get("model_profile_hash"):
                raise ResearchDashboardError("variant model identity mismatch")
            if key not in manifest.get("skill_snapshot_sha256s", {}):
                raise ResearchDashboardError("variant Skill identity is not in the batch")
            for audit in result.get("model_call_audits", []):
                cache_key = str(audit.get("cache_key", ""))
                document_hash = str(audit.get("cache_document_sha256", ""))
                if len(cache_key) != 64 or len(document_hash) != 64:
                    raise ResearchDashboardError("variant model call identity is incomplete")
                previous = expected_model_calls.setdefault(cache_key, document_hash)
                if previous != document_hash:
                    raise ResearchDashboardError("variant model call hash conflict")
            variants[key] = result
        actual_model_calls = {
            row["cache_key"]: row["cache_document_sha256"] for row in model_calls
        }
        if actual_model_calls != expected_model_calls:
            raise ResearchDashboardError("batch model call snapshot set is incomplete")
        if status:
            if set(status.get("completed_variants", [])) != set(variants):
                raise ResearchDashboardError("batch completion status differs from results")
            if status.get("skill_snapshot_count") != len(skill_snapshots):
                raise ResearchDashboardError("batch Skill count mismatch")
            if status.get("model_call_document_count") != len(model_calls):
                raise ResearchDashboardError("batch model call count mismatch")
        from .replay_summary import candidate_summary, compare_versions
        labels = _verified_labels(root / "labels.json")
        summaries = {key: {symbol: candidate_summary(value, symbol, labels.get('labels', {}).get(symbol, {}))
                           for symbol in value.get('reports_by_symbol', {})}
                     for key, value in variants.items()}
        comparisons = {a: {b: compare_versions(left, right, skill_snapshots[a]['documents'], skill_snapshots[b]['documents'])
                           for b, right in variants.items() if a != b} for a, left in variants.items()}
        return {
            "batch_id": batch_id, "manifest": manifest, "status": status,
            "replay_summaries": summaries, "version_comparisons": comparisons,
            "evidence": {
                "evidence_hash": evidence.manifest["evidence_hash"],
                "cutoff_status": evidence.manifest["cutoff_status"],
                "as_of_et": evidence.manifest["as_of_et"],
                "provider_manifest": evidence.manifest["provider_manifest"],
                "candidates": evidence.candidate_intake["candidates"],
                "catalog": [{
                    "evidence_id": row.get("evidence_id"),
                    "domain": row.get("domain"),
                    "provider": row.get("provider"),
                    "source_uri": row.get("source_uri"),
                    "captured_at": row.get("captured_at"),
                    "published_at": row.get("published_at"),
                    "scope_symbols": row.get("scope_symbols", []),
                    "raw_sha256": row.get("raw_sha256"),
                } for row in evidence.manifest.get("records", [])],
            },
            "variants": variants,
            "labels": labels,
            "skill_snapshots": skill_snapshots,
            "model_calls": model_calls,
        }

    def export_professor_report(self, destination: Path) -> Path:
        overview = self.overview()
        rows = []
        for batch in overview["batches"]:
            if not batch["source_valid"]:
                rows.append(
                    "<tr>"
                    f"<td>{html.escape(batch['trade_date'])}</td>"
                    "<td>—</td><td>工程记录不可完整核验</td>"
                    "<td>不纳入统计</td>"
                    f"<td>{html.escape(batch['cutoff_status'])}</td>"
                    "</tr>"
                )
                continue
            try:
                detail = self.batch_detail(batch["batch_id"])
            except (ResearchDashboardError, ResearchBatchError):
                continue
            labels = detail.get("labels", {}).get("labels", {})
            for key, variant in detail["variants"].items():
                prediction_rows = []
                result_rows = []
                for prediction in variant.get("predictions", []):
                    symbol = prediction["symbol"]
                    predicted = "看涨" if prediction["direction"] == "bullish" else "看跌"
                    prediction_rows.append(f"{symbol} {predicted}")
                    label = labels.get(symbol, {})
                    if label.get("status") != "final":
                        result_rows.append(f"{symbol} 待核验")
                        continue
                    actual = {
                        "bullish": "看涨", "bearish": "看跌", "neutral": "平盘",
                    }.get(label.get("actual_direction"), "未知")
                    return_pct = float(label.get("open_to_close_return", 0.0)) * 100
                    mark = "正确" if prediction["direction"] == label.get("actual_direction") else "错误"
                    result_rows.append(
                        f"{symbol} 实际{actual} {return_pct:+.2f}%（{mark}）"
                    )
                predictions = "、".join(prediction_rows) or "空榜"
                outcomes = "；".join(result_rows) if result_rows else "无预测，不记分"
                rows.append(
                    "<tr>"
                    f"<td>{html.escape(batch['trade_date'])}</td>"
                    f"<td>{html.escape(variant['variant']['label'])}</td>"
                    f"<td>{html.escape(predictions)}</td>"
                    f"<td>{html.escape(outcomes)}</td>"
                    f"<td>{html.escape(detail['evidence']['cutoff_status'])}</td>"
                    "</tr>"
                )
        performance = []
        for row in overview.get("performance", []):
            evaluated = int(row.get("evaluated") or 0)
            correct = int(row.get("correct") or 0)
            rate = f"{correct / evaluated:.1%}" if evaluated else "尚无已核验预测"
            performance.append(
                f"<li>{html.escape(row['variant_key'])}：{correct}/{evaluated}，{rate}</li>"
            )
        page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>SHAQ Daily Oracle Lab 研究报告</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:42px;color:#0b2341}}
h1{{color:#1769aa}}table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;border-bottom:1px solid #dbe5ee;text-align:left}}
.note{{padding:14px;background:#eef6fc;border-left:4px solid #1769aa}}</style>
<h1>SHAQ Daily Oracle Lab 研究记录</h1>
<p class="note">本报告只展示本地 Shadow 研究判断，不含账户、密钥、本地路径、原始付费数据或真实订单。</p>
<h2>已核验表现</h2><ul>{''.join(performance) or '<li>尚无已核验预测</li>'}</ul>
<table><thead><tr><th>日期</th><th>Skill版本</th><th>0–3只结果</th><th>盘后核验</th><th>截止状态</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="5">尚无研究批次</td></tr>'}</tbody></table></html>"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(page, encoding="utf-8")
        return destination
