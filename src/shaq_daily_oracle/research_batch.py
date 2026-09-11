from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from zoneinfo import ZoneInfo

from filelock import FileLock

from .contracts import DOMAINS, validate_adversary_report, validate_domain_report
from .decision_sandbox import build_decision_input, execute_decision_script, decision_parameters, decision_mode
from .hashing import sha256_file, sha256_payload
from .lineage import build_lineage_graph
from .model_backends import ModelProfile, call_structured
from .research_progress import safe_observe
from .sandboxed_codex import (
    DOMAIN_SKILLS,
    _adversary_schema,
    _bind_verified_lineage,
    _report_schema,
    integration_audit,
)
from .skill_versions import LocalSkillRegistry, parse_agent_profile


class ResearchBatchError(ValueError):
    """A research batch cannot be reproduced from its immutable inputs."""


HORIZON = "official_US_regular_session_open_to_close"


@dataclass(frozen=True)
class VariantSelection:
    version_id: str
    author: str
    label: str
    version_sha256: str
    source_commit_sha: str

    @classmethod
    def from_registry_row(cls, row: dict[str, Any]) -> "VariantSelection":
        version_id = str(row.get("version_id", ""))
        author = str(row.get("author", "team"))
        digest = str(row.get("version_sha256", ""))
        commit = str(row.get("source_commit_sha") or row.get("base_main_sha") or "bundled")
        if not version_id or len(digest) != 64:
            raise ResearchBatchError("selected Skill version has no immutable identity")
        return cls(
            version_id=version_id,
            author=author,
            label=str(row.get("label") or f"{author} · {version_id}"),
            version_sha256=digest,
            source_commit_sha=commit,
        )

    def identity(self) -> str:
        return sha256_payload(asdict(self))


@dataclass(frozen=True)
class FrozenEvidence:
    root: Path
    manifest: dict[str, Any]
    lineage: dict[str, Any]
    candidate_intake: dict[str, Any]


def _application_version() -> str:
    try:
        return version("shaq-daily-oracle")
    except PackageNotFoundError:
        return "source-tree"


def _safe_name(value: str) -> str:
    output = "".join(character.lower() if character.isalnum() else "-" for character in value)
    output = "-".join(part for part in output.split("-") if part)
    if not output:
        raise ResearchBatchError("empty filesystem identity")
    return output


def _write_json_once(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise ResearchBatchError(f"immutable artifact already exists: {path.name}") from exc


def _write_json_same_or_once(path: Path, value: dict[str, Any]) -> None:
    """Create an immutable JSON artifact, accepting only byte-identical reuse."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with FileLock(str(path) + ".write.lock"):
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != payload:
                raise ResearchBatchError(
                    f"immutable artifact differs from its existing bytes: {path.name}"
                )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def freeze_evidence_bundle(
    *,
    root: Path,
    as_of_et: str,
    scheduled_cutoff_et: str,
    cutoff_status: str,
    candidates: list[dict[str, Any]],
    records: list[dict[str, Any]],
    files: dict[str, bytes],
    provider_manifest: dict[str, Any],
) -> FrozenEvidence:
    """Write one immutable evidence snapshot shared by every selected variant."""

    if cutoff_status not in {"on_time", "late_research_only"}:
        raise ResearchBatchError("invalid evidence cutoff status")
    if root.exists():
        return load_frozen_evidence(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=root.parent))
    try:
        file_hashes: dict[str, str] = {}
        for relative_path, content in sorted(files.items()):
            pure = PurePosixPath(relative_path)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise ResearchBatchError("evidence file path is unsafe")
            destination = temporary / pure
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            file_hashes[pure.as_posix()] = sha256_file(destination)
        normalized_records = []
        for record in records:
            normalized = dict(record)
            path = PurePosixPath(str(normalized.get("raw_file_path", "")))
            if path.as_posix() not in file_hashes:
                raise ResearchBatchError("lineage record points outside the frozen files")
            normalized["raw_file_path"] = path.as_posix()
            normalized["raw_sha256"] = file_hashes[path.as_posix()]
            normalized_records.append(normalized)
        candidate_intake = {
            "schema_version": 1,
            "method": "shared_point_in_time_research_candidate_set",
            "candidates": sorted(candidates, key=lambda row: str(row["symbol"])),
        }
        unsigned = {
            "schema_version": 1,
            "as_of_et": as_of_et,
            "scheduled_cutoff_et": scheduled_cutoff_et,
            "cutoff_status": cutoff_status,
            "prediction_target": HORIZON,
            "provider_manifest": provider_manifest,
            "candidate_intake": candidate_intake,
            "candidate_set_sha256": sha256_payload(candidate_intake),
            "records": normalized_records,
            "file_sha256": file_hashes,
        }
        manifest = {**unsigned, "evidence_hash": sha256_payload(unsigned)}
        _write_json_once(temporary / "evidence_manifest.json", manifest)
        temporary.replace(root)
    except Exception:
        import shutil

        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return load_frozen_evidence(root)


def load_frozen_evidence(root: Path) -> FrozenEvidence:
    manifest_path = root / "evidence_manifest.json"
    if not manifest_path.is_file():
        raise ResearchBatchError("frozen evidence manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = str(manifest.get("evidence_hash", ""))
    unsigned = {key: value for key, value in manifest.items() if key != "evidence_hash"}
    if declared != sha256_payload(unsigned):
        raise ResearchBatchError("frozen evidence manifest hash mismatch")
    for relative_path, digest in manifest.get("file_sha256", {}).items():
        pure = PurePosixPath(str(relative_path))
        if pure.is_absolute() or ".." in pure.parts:
            raise ResearchBatchError("frozen evidence contains an unsafe path")
        path = (root / pure).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ResearchBatchError("frozen evidence path escapes its root") from exc
        if not path.is_file() or sha256_file(path) != digest:
            raise ResearchBatchError("frozen evidence file hash mismatch")
    records = []
    for record in manifest.get("records", []):
        normalized = dict(record)
        normalized["raw_file_path"] = str((root / normalized["raw_file_path"]).resolve())
        records.append(normalized)
    lineage = build_lineage_graph(records, root, str(manifest["as_of_et"]))
    candidate_intake = dict(manifest["candidate_intake"])
    if sha256_payload(candidate_intake) != manifest.get("candidate_set_sha256"):
        raise ResearchBatchError("candidate set hash mismatch")
    return FrozenEvidence(
        root=root.resolve(), manifest=manifest, lineage=lineage,
        candidate_intake=candidate_intake,
    )


def _evidence_content(path: str, expected_hash: str) -> Any:
    source = Path(path)
    if sha256_file(source) != expected_hash:
        raise ResearchBatchError("evidence changed before model packet construction")
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"content_type": "binary_or_text_source", "sha256": expected_hash}


def market_input_view(content):
    """Benchmark context uses the full close/volume path; raw OHLC stays archived.

    Individual stock OHLC is untouched. This is a deterministic input view,
    not an AI summary, a sample of dates, or a new independent evidence root.
    """
    if not isinstance(content, dict) or 'daily_and_premarket' not in content:
        return content
    result = json.loads(json.dumps(content))
    for state in result['daily_and_premarket'].values():
        daily = state.get('daily', {})
        bars = daily.pop('bars', [])
        daily['path'] = {field: [row.get(field) for row in bars] for field in ('timestamp', 'close', 'volume')}
        daily['view'] = 'all_dates_unadjusted_close_and_volume; full_OHLC_in_original_source'
    return result


def _tasks_for_domain(evidence: FrozenEvidence, domain: str) -> list[dict[str, Any]]:
    records = evidence.lineage["records"]
    declared_statuses = {
        (str(row.get("symbol", "*")).upper(), str(row.get("domain", ""))): str(
            row.get("status", "")
        )
        for row in evidence.manifest.get("provider_manifest", {}).get(
            "collection_statuses", []
        )
    }
    tasks = []
    for candidate in evidence.candidate_intake["candidates"]:
        symbol = str(candidate["symbol"]).upper()
        applicable = []
        for record in records:
            consumers = set(record.get("consumer_domains", [record["domain"]]))
            scopes = {str(value).upper() for value in record.get("scope_symbols", ["*"])}
            if domain not in consumers or not ({symbol, "*"} & scopes):
                continue
            applicable.append({
                "evidence_id": record["evidence_id"],
                "provider": record["provider"],
                "source_uri": record["source_uri"],
                "captured_at": record["captured_at"],
                "lineage_root_ids": evidence.lineage["evidence_to_roots"][record["evidence_id"]],
                "content": market_input_view(_evidence_content(record["raw_file_path"], record["raw_sha256"])),
            })
        own = [row for row in records if row["domain"] == domain and (
            "*" in {str(value).upper() for value in row.get("scope_symbols", ["*"])}
            or symbol in {str(value).upper() for value in row.get("scope_symbols", [])}
        )]
        declared = declared_statuses.get((symbol, domain)) or declared_statuses.get(("*", domain))
        if own:
            collection_status = "collected"
        elif declared in {"no_data", "not_entitled", "provider_error", "not_applicable"}:
            collection_status = declared
        elif domain == "event":
            collection_status = "not_applicable"
        else:
            collection_status = "no_data"
        tasks.append({
            "task_id": "task_" + sha256_payload({
                "evidence_hash": evidence.manifest["evidence_hash"],
                "symbol": symbol,
                "domain": domain,
            })[:20],
            "symbol": symbol,
            "domain": domain,
            "as_of_et": evidence.manifest["as_of_et"],
            "horizon": HORIZON,
            "collection_status": collection_status,
            "evidence": sorted(applicable, key=lambda row: row["evidence_id"]),
        })
    return tasks


def _deterministic_empty_report(task: dict[str, Any]) -> dict[str, Any]:
    status = str(task["collection_status"])
    component = {
        "market": "market_beta", "relationships": "industry_spillover",
        "event": "company_event", "capital": "capital_flow",
        "derivatives": "derivatives_distribution", "price_volume": "price_volume_state",
    }[task["domain"]]
    if status == "not_applicable":
        availability, verdict = "available", "not_applicable"
        thesis = "截止时间前没有适用于该候选的合格公司事件。"
    else:
        availability, verdict = status if status in {
            "no_data", "not_entitled", "provider_error"
        } else "no_data", "unavailable"
        thesis = "该数据配置没有提供本领域所需的合格证据。"
    return {
        "domain": task["domain"], "as_of_et": task["as_of_et"],
        "horizon": task["horizon"], "availability": availability,
        "verdict": verdict, "component_type": component, "thesis": thesis,
        "antithesis": "没有证据时不把其他领域的信号伪装成本领域判断。",
        "unknowns": ["本领域方向未知"],
        "invalidation": [], "evidence_ids": [], "lineage_root_ids": [],
    }


class ContentAddressedModelCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._rate_lock = threading.Lock()
        self._call_times: dict[str, deque[float]] = defaultdict(deque)

    def _wait_for_rate_slot(self, profile: ModelProfile) -> None:
        """Reserve one request in the configured rolling one-minute window."""

        identity = profile.identity()
        while True:
            with self._rate_lock:
                now = time.monotonic()
                calls = self._call_times[identity]
                while calls and now - calls[0] >= 60:
                    calls.popleft()
                if len(calls) < profile.rate_limit_per_minute:
                    calls.append(now)
                    return
                delay = max(0.01, 60 - (now - calls[0]))
            time.sleep(delay)

    def call(
        self,
        *,
        profile: ModelProfile,
        secret: str,
        prompt: str,
        schema: dict[str, Any],
        caller: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = call_structured,
        on_model_start: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        key_document = {
            "cache_schema_version": 2,
            "profile_sha256": profile.identity(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": sha256_payload(schema),
        }
        cache_key = sha256_payload(key_document)
        path = self.root / f"{cache_key}.json"
        lock = FileLock(str(path) + ".lock")
        with lock:
            if path.is_file():
                document = self._verified_document(path, cache_key=cache_key)
                if document["prompt"] != prompt or document["schema"] != schema:
                    raise ResearchBatchError("model cache input differs from its content key")
                return document["result"], self._public_audit(document), True
            self._wait_for_rate_slot(profile)
            if on_model_start:
                on_model_start()
            result, audit = caller(
                profile=profile, secret=secret, prompt=prompt, schema=schema
            )
            unsigned = {
                "schema_version": 2, "cache_key": cache_key,
                "key_document": key_document, "prompt": prompt, "schema": schema,
                "result": result, "result_sha256": sha256_payload(result),
                "audit": audit, "audit_sha256": sha256_payload(audit),
            }
            document = {
                **unsigned, "cache_document_sha256": sha256_payload(unsigned)
            }
            _write_json_once(path, document)
            return result, self._public_audit(document), False

    @staticmethod
    def _public_audit(document: dict[str, Any]) -> dict[str, Any]:
        return {
            **document["audit"],
            "cache_key": document["cache_key"],
            "cache_document_sha256": document["cache_document_sha256"],
        }

    @staticmethod
    def _verified_document(path: Path, *, cache_key: str) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchBatchError("model cache document is unreadable") from exc
        declared = document.get("cache_document_sha256")
        unsigned = {
            key: value for key, value in document.items()
            if key != "cache_document_sha256"
        }
        if document.get("cache_key") != cache_key or declared != sha256_payload(unsigned):
            raise ResearchBatchError("model cache identity mismatch")
        key_document = document.get("key_document")
        prompt = document.get("prompt")
        schema = document.get("schema")
        if not isinstance(key_document, dict) or not isinstance(prompt, str) or not isinstance(schema, dict):
            raise ResearchBatchError("model cache lacks its exact frozen input")
        if key_document != {
            "cache_schema_version": 2,
            "profile_sha256": key_document.get("profile_sha256"),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": sha256_payload(schema),
        }:
            raise ResearchBatchError("model cache input hash mismatch")
        if sha256_payload(key_document) != cache_key:
            raise ResearchBatchError("model cache content key mismatch")
        if sha256_payload(document.get("result")) != document.get("result_sha256"):
            raise ResearchBatchError("model cache result hash mismatch")
        if sha256_payload(document.get("audit")) != document.get("audit_sha256"):
            raise ResearchBatchError("model cache audit hash mismatch")
        return document

    def snapshot_calls(
        self, audits: list[dict[str, Any]], *, destination: Path
    ) -> list[str]:
        """Copy exact, verified model inputs and outputs into one immutable batch."""

        keys = sorted({str(row.get("cache_key", "")) for row in audits})
        if not keys or any(len(key) != 64 for key in keys):
            raise ResearchBatchError("model call audit has no content-addressed identity")
        for key in keys:
            source = self.root / f"{key}.json"
            document = self._verified_document(source, cache_key=key)
            _write_json_same_or_once(destination / f"{key}.json", document)
        return keys


def shared_task_inputs(tasks):
    pool, compact = {}, []
    for task in tasks:
        refs = []
        for record in task['evidence']:
            key = sha256_payload(record['content'])
            pool[key] = record['content']
            refs.append({**{k:v for k,v in record.items() if k != 'content'}, 'content_ref': key})
        compact.append({**task, 'evidence': refs})
    return pool, compact


def _domain_prompt(
    *, domain: str, tasks: list[dict[str, Any]], documents: dict[str, str]
) -> str:
    skill_name = DOMAIN_SKILLS[domain]
    skill = documents[f"skills/{skill_name}/SKILL.md"]
    foundation = documents.get(f"skills/{skill_name}/references/foundations.md", "")
    role_card = documents.get(f"skills/{skill_name}/agents/openai.yaml", "")
    role = parse_agent_profile(role_card) if role_card else {}
    pool, tasks = shared_task_inputs(tasks)
    component_instruction = (
        "This version uses final synthesis: your verdict describes only your domain's supported "
        "contribution, not an independently sufficient whole-stock forecast. Partial data limits the "
        "claim, not automatically the entire report. Do not require observing the future opening price "
        "or proving complete absorption. Distinguish a supported conditional mechanism from certainty. "
        "Missing indispensable data still requires unavailable; balanced evidence still permits neutral. "
    ) if decision_mode(documents["decision/cases.json"]) == "synthesis" else ""
    return (
        "You are one isolated SHAQ Daily Oracle research-domain analyst. "
        "Use only the frozen packet below. Do not browse, call tools, add remembered company facts, "
        "or infer missing measurements. Analyze each task independently for the official US regular "
        "session open-to-close horizon. Write thesis, antithesis, unknowns and invalidation in concise "
        "Simplified Chinese. Cite only evidence_ids present in that task. Return one result for every "
        "task_id and no others. The program will replace lineage_root_ids from verified evidence. "
        "Do not output probability, confidence, strength, score, ranking, labels or outcomes.\n\n"
        f"{component_instruction}"
        f"ROLE CARD:\n{role_card}\n\n"
        f"ROLE TASK: {role.get('default_prompt', '')}\n\n"
        f"SKILL:\n{skill}\n\nFOUNDATIONS:\n{foundation}\n\n"
        "Each task's content_ref resolves to the shared evidence table below. Use only references assigned to that task.\n"
        f"SHARED EVIDENCE:\n{json.dumps(pool, sort_keys=True, ensure_ascii=False)}\n\n"
        f"FROZEN TASKS:\n{json.dumps(tasks, sort_keys=True, ensure_ascii=False)}"
    )


def _run_domain(
    *,
    domain: str,
    evidence: FrozenEvidence,
    documents: dict[str, str],
    profile: ModelProfile,
    secret: str,
    cache: ContentAddressedModelCache,
    caller: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
    observer: Callable[..., None] | None = None,
    batch_id: str = "",
    variant_key: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    domain_started = time.monotonic()
    tasks = _tasks_for_domain(evidence, domain)
    from .module_rules import default_rule, execute_rule
    code = documents.get(f"modules/{domain}/compute.js")
    if code and code != default_rule(domain):
        for task in tasks:
            task["computed_features"] = execute_rule(code, {"evidence": task["evidence"], "symbol": task["symbol"]})
    all_tasks = tasks
    empty_reports = {task['task_id']: _deterministic_empty_report(task) for task in tasks
                     if not task['evidence'] or task['collection_status'] != 'collected'}
    tasks = [task for task in tasks if task['task_id'] not in empty_reports]
    if not tasks:
        for task in all_tasks:
            report = empty_reports[task['task_id']]
            safe_observe(observer, stage="report_validated", batch_id=batch_id,
                         variant_key=variant_key, symbol=task["symbol"], domain=domain,
                         status="no_data", elapsed_seconds=round(time.monotonic()-domain_started, 3),
                         report={**report, "original": report, "evidence": []})
        return [empty_reports[task['task_id']] for task in all_tasks], []
    groups, group = [], []
    for task in tasks:
        prompt = _domain_prompt(domain=domain, tasks=group + [task], documents=documents)
        if group and len(prompt.encode('utf-8')) // 4 + profile.maximum_output_tokens > profile.maximum_context_tokens:
            groups.append(group)
            group = []
        group.append(task)
    if group:
        groups.append(group)
    rows, audits, call_by_task = [], [], {}
    for group in groups:
        schema = _report_schema()
        schema['properties']['results']['items']['properties']['task_id']['enum'] = [task['task_id'] for task in group]
        prompt = _domain_prompt(domain=domain, tasks=group, documents=documents)
        call_id = sha256_payload({"cache_schema_version": 2, "profile_sha256": profile.identity(), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "schema_sha256": sha256_payload(schema)})
        for task in group:
            call_by_task[task["task_id"]] = (call_id, None)
        started = time.monotonic()
        requested = safe_observe(observer, stage="call_requested", batch_id=batch_id,
                     variant_key=variant_key, domain=domain,
                     symbols=[task["symbol"] for task in group], call_id=call_id,
                     status="requested", elapsed_seconds=0.0)
        attempt = requested.get("attempt", 1) if isinstance(requested, dict) else 1
        for task in group:
            call_by_task[task["task_id"]] = (call_id, attempt)
        def model_start() -> None:
            safe_observe(observer, stage="model_started", batch_id=batch_id,
                         variant_key=variant_key, domain=domain,
                         symbols=[task["symbol"] for task in group], call_id=call_id,
                         attempt=attempt, status="running", elapsed_seconds=round(time.monotonic()-started, 3))
        try:
            result, audit, cache_hit = cache.call(profile=profile, secret=secret,
                prompt=prompt, schema=schema, caller=caller, on_model_start=model_start)
        except Exception as exc:
            safe_observe(observer, stage="failure", batch_id=batch_id,
                         variant_key=variant_key, domain=domain,
                         symbols=[task["symbol"] for task in group], call_id=call_id,
                         attempt=attempt, status="failed", error_type=type(exc).__name__,
                         message=str(exc), elapsed_seconds=round(time.monotonic()-started, 3))
            raise
        safe_observe(observer, stage="cache_hit" if cache_hit else "model_returned", batch_id=batch_id,
                     variant_key=variant_key, domain=domain,
                     symbols=[task["symbol"] for task in group], call_id=call_id,
                     attempt=attempt, status="cache_hit" if cache_hit else "complete",
                     elapsed_seconds=round(time.monotonic() - started, 3))
        group_rows = result.get('results') if isinstance(result, dict) else None
        if not isinstance(group_rows, list):
            raise ResearchBatchError(f"{domain} model output has no result list")
        rows.extend(group_rows)
        audits.append({**audit, 'domain': domain, 'cache_hit': cache_hit})
    by_task = {str(row.get("task_id")): row.get("report") for row in rows if isinstance(row, dict)}
    if len(rows) != len(tasks) or set(by_task) != {task["task_id"] for task in tasks}:
        raise ResearchBatchError(f"{domain} model output does not match frozen tasks")
    evidence_domains = {
        record["evidence_id"]: set(record.get("consumer_domains", [record["domain"]]))
        for record in evidence.lineage["records"]
    }
    reports = []
    for task in tasks:
        raw = by_task[task["task_id"]]
        if not isinstance(raw, dict):
            raise ResearchBatchError(f"{domain} returned a non-object report")
        if raw.get("domain") != domain or raw.get("as_of_et") != task["as_of_et"]:
            raise ResearchBatchError(f"{domain} changed its frozen task identity")
        bound = _bind_verified_lineage(raw, evidence.lineage["evidence_to_roots"])
        try:
            reports.append(validate_domain_report(
                bound, evidence.lineage["evidence_to_roots"], evidence_domains
            ))
        except Exception as exc:
            safe_observe(observer, stage="validation_failure", batch_id=batch_id,
                         variant_key=variant_key, symbol=task["symbol"], domain=domain,
                         call_id=call_by_task.get(task["task_id"], ("", 1))[0],
                         attempt=call_by_task.get(task["task_id"], ("", 1))[1],
                         status="failed", error_type=type(exc).__name__, message=str(exc),
                         elapsed_seconds=round(time.monotonic()-domain_started, 3))
            raise
        report = reports[-1]
        catalog = {row["evidence_id"]: row for row in evidence.lineage["records"]}
        observed = {row["evidence_id"]: row.get("content") for row in task["evidence"]}
        safe_observe(observer, stage="report_validated", batch_id=batch_id,
                     variant_key=variant_key, symbol=task["symbol"], domain=domain,
                     call_id=call_by_task.get(task["task_id"], ("", 1))[0],
                     attempt=call_by_task.get(task["task_id"], ("", 1))[1],
                     status="validated", report={**report, "original": report,
                         "evidence": [{**{key: catalog[eid].get(key) for key in
                             ("evidence_id", "provider", "source_uri", "captured_at", "unit")},
                             "observed": observed.get(eid)}
                             for eid in report.get("evidence_ids", []) if eid in catalog]})
    valid_reports = {task['task_id']: report for task, report in zip(tasks, reports, strict=True)}
    valid_reports.update(empty_reports)
    for task in all_tasks:
        if task["task_id"] not in empty_reports:
            continue
        report = empty_reports[task["task_id"]]
        safe_observe(observer, stage="report_validated", batch_id=batch_id,
                     variant_key=variant_key, symbol=task["symbol"], domain=domain,
                     status="no_data", elapsed_seconds=round(time.monotonic()-domain_started, 3),
                     report={**report, "original": report, "evidence": []})
    return [valid_reports[task['task_id']] for task in all_tasks], audits


def _adversary_prompt(
    *, reports_by_symbol: dict[str, list[dict[str, Any]]], documents: dict[str, str]
) -> str:
    skill = documents["skills/thesis-adversary/SKILL.md"]
    foundation = documents.get("skills/thesis-adversary/references/foundations.md", "")
    role_card = documents.get("skills/thesis-adversary/agents/openai.yaml", "")
    role = parse_agent_profile(role_card) if role_card else {}
    integrity_boundary = (
        "In this version domain verdicts are components, not standalone all-day predictions. "
        "Ordinary uncertainty, absent consensus, opposing mechanisms and unknown opening absorption "
        "belong in strongest_countercase, not veto. Veto only corrupted provenance, leaked results, "
        "fabricated facts or a report that actually substitutes a different target period. "
    ) if decision_mode(documents["decision/cases.json"]) == "synthesis" else ""
    return (
        "You are the non-voting SHAQ adversary. Audit only the supplied completed reports. "
        "Do not add evidence, use outside facts, browse, call tools or choose a stock direction. "
        "Return one result for every symbol and no others. A veto is only for integrity, leakage, "
        "unsupported facts or an unresolved contradiction, not ordinary uncertainty. Write the "
        "countercase and veto reason in concise Simplified Chinese.\n\n"
        f"{integrity_boundary}"
        f"ROLE CARD:\n{role_card}\n\n"
        f"ROLE TASK: {role.get('default_prompt', '')}\n\n"
        f"SKILL:\n{skill}\n\nFOUNDATIONS:\n{foundation}\n\n"
        f"REPORTS:\n{json.dumps(reports_by_symbol, sort_keys=True, ensure_ascii=False)}"
    )


def run_variant(
    *,
    variant: VariantSelection,
    evidence: FrozenEvidence,
    registry: LocalSkillRegistry,
    profile: ModelProfile,
    secret: str,
    cache: ContentAddressedModelCache,
    integration_policy: dict[str, Any],
    output_root: Path,
    caller: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = call_structured,
    observer: Callable[..., None] | None = None,
    batch_id: str = "",
) -> dict[str, Any]:
    variant_key = f"{variant.author}/{variant.version_id}"
    stage_started = time.monotonic()
    safe_observe(observer, stage="preparation", batch_id=batch_id,
                 variant_key=variant_key, status="complete", elapsed_seconds=round(time.monotonic()-stage_started, 3))
    variant_root = output_root / f"{_safe_name(variant.author)}--{_safe_name(variant.version_id)}"
    result_path = variant_root / "variant_result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        declared = result.get("variant_result_sha256")
        unsigned = {key: value for key, value in result.items() if key != "variant_result_sha256"}
        if declared != sha256_payload(unsigned):
            raise ResearchBatchError("stored variant result hash mismatch")
        safe_observe(observer, stage="variant_reused", batch_id=batch_id,
                     variant_key=variant_key, symbols=sorted(result.get("reports_by_symbol", {})),
                     status="complete", elapsed_seconds=round(time.monotonic()-stage_started, 3))
        for symbol, reports in result.get("reports_by_symbol", {}).items():
            for report in reports:
                safe_observe(observer, stage="report_validated", batch_id=batch_id,
                             variant_key=variant_key, symbol=symbol, domain=report.get("domain", ""),
                             status="reused", elapsed_seconds=0.0,
                             report={**report, "original": report, "evidence": []})
        return result
    documents = registry.effective_skills(variant.version_id, variant.author)
    screening_path = evidence.root / "raw/screening.json"
    if screening_path.is_file():
        from .module_rules import default_rule
        packet = json.loads(screening_path.read_text(encoding="utf-8"))
        script = documents.get("modules/screening/compute.js", default_rule("screening"))
        symbols = packet["candidate_sets"].get(sha256_payload(script))
        if symbols is None:
            raise ResearchBatchError("此筛选版本没有共享的候选证据，请重新采集")
        evidence = replace(evidence, candidate_intake={**evidence.candidate_intake, "candidates": [c for c in evidence.candidate_intake["candidates"] if c["symbol"] in symbols]})
    safe_observe(observer, stage="screening", batch_id=batch_id,
                 variant_key=variant_key,
                 symbols=[str(row["symbol"]).upper() for row in evidence.candidate_intake["candidates"]],
                 status="complete", elapsed_seconds=round(time.monotonic()-stage_started, 3))
    reports_by_symbol = {
        str(row["symbol"]).upper(): [] for row in evidence.candidate_intake["candidates"]
    }
    audits = []
    for domain in sorted(DOMAINS):
        reports, domain_audits = _run_domain(
            domain=domain, evidence=evidence, documents=documents,
            profile=profile, secret=secret, cache=cache, caller=caller,
            observer=observer, batch_id=batch_id, variant_key=variant_key,
        )
        for candidate, report in zip(evidence.candidate_intake["candidates"], reports, strict=True):
            reports_by_symbol[str(candidate["symbol"]).upper()].append(report)
        audits.extend(domain_audits)
    adversary_prompt = _adversary_prompt(
        reports_by_symbol=reports_by_symbol, documents=documents
    )
    if reports_by_symbol:
        adversary_started = time.monotonic()
        safe_observe(observer, stage="adversary", batch_id=batch_id,
                     variant_key=variant_key, symbols=sorted(reports_by_symbol), status="running")
        adversary_result, adversary_audit, cache_hit = cache.call(
            profile=profile, secret=secret, prompt=adversary_prompt,
            schema=_adversary_schema(), caller=caller,
        )
    else:
        adversary_result, adversary_audit, cache_hit = {"results": []}, {}, False
    adversary_rows = adversary_result.get("results") if isinstance(adversary_result, dict) else None
    if not isinstance(adversary_rows, list):
        raise ResearchBatchError("adversary output has no result list")
    adversary_by_symbol = {}
    for row in adversary_rows:
        if not isinstance(row, dict):
            raise ResearchBatchError("adversary returned a non-object result")
        symbol = str(row.get("symbol", "")).upper()
        if symbol in adversary_by_symbol:
            raise ResearchBatchError("adversary returned a duplicate symbol")
        adversary_by_symbol[symbol] = validate_adversary_report(row.get("report", {}))
    if set(adversary_by_symbol) != set(reports_by_symbol):
        raise ResearchBatchError("adversary output does not match frozen candidates")
    safe_observe(observer, stage="adversary", batch_id=batch_id,
                 variant_key=variant_key, symbols=sorted(reports_by_symbol), status="complete",
                 elapsed_seconds=round(time.monotonic() - adversary_started, 3) if reports_by_symbol else 0.0)
    if reports_by_symbol:
        audits.append({**adversary_audit, "domain": "adversary", "cache_hit": cache_hit})
    del integration_policy  # Decision policy is versioned code, not a hidden Python threshold.
    decision_input = build_decision_input(
        as_of_et=str(evidence.manifest["as_of_et"]),
        reports_by_symbol=reports_by_symbol,
        adversary_by_symbol=adversary_by_symbol,
        candidate_intake=evidence.candidate_intake,
        root_component_types=evidence.lineage["root_component_types"],
    )
    mode = decision_mode(documents["decision/cases.json"])
    parameters = decision_parameters(documents["decision/cases.json"])
    decision_input["research_parameters"] = parameters
    synthesis = None
    if mode == "synthesis" and reports_by_symbol:
        from .synthesis import synthesis_prompt, synthesis_schema, validate_synthesis
        synthesis, synthesis_audit, cache_hit = cache.call(
            profile=profile, secret=secret, caller=caller,
            prompt=synthesis_prompt(reports=reports_by_symbol, adversary=adversary_by_symbol,
                documents=documents, as_of_et=evidence.manifest["as_of_et"],
                maximum_predictions=parameters["maximum_predictions"]),
            schema=synthesis_schema(list(reports_by_symbol)),
        )
        decision_input["synthesis"] = validate_synthesis(synthesis, reports_by_symbol,
            evidence.lineage["evidence_to_roots"], maximum_predictions=parameters["maximum_predictions"])
        audits.append({**synthesis_audit, "domain": "synthesis", "cache_hit": cache_hit})
    elif mode == "synthesis":
        synthesis = {"decisions": []}
        decision_input["synthesis"] = {"schema_version": 1, "decisions": []}
    decision_result = execute_decision_script(script=documents["decision/decision.js"],
        decision_input=decision_input, citation_policy="available" if mode == "synthesis" else "directional")
    predictions = decision_result["predictions"]
    activation_path = (output_root.parents[2] / 'virtual_accounts' /
                       'zipline_minute_v2' / 'activation.json')
    if activation_path.is_file():
        activation = json.loads(activation_path.read_text(encoding='utf-8'))
        risk_rules = activation.get('rules', {})
        frozen_at = str(evidence.manifest['as_of_et'])
        if (risk_rules.get('risk_fraction') is not None
                and datetime.fromisoformat(frozen_at) >= datetime.fromisoformat(activation['activated_at'])):
            from .virtual_accounts import freeze_risk_sizing
            histories = {}
            for prediction in predictions:
                source = evidence.root / 'raw' / 'stocks' / f"{prediction['symbol']}.json"
                try:
                    payload = json.loads(source.read_text(encoding='utf-8'))
                except (FileNotFoundError, json.JSONDecodeError):
                    payload = {}
                histories[prediction['symbol']] = payload.get('daily', {}).get('bars', [])
            predictions = freeze_risk_sizing(
                predictions, histories, trade_date=frozen_at[:10], frozen_at_et=frozen_at,
                lookback=int(risk_rules['lookback']), policy_sha256=sha256_payload(activation))
    audit_by_symbol = integration_audit(
        reports_by_symbol=reports_by_symbol,
        adversary_by_symbol=adversary_by_symbol,
        predictions=predictions,
    )
    for row in decision_result["decisions"]:
        audit = audit_by_symbol[row["symbol"]]
        audit["decision_reason"] = row["reason"]
        audit["decision_evidence_root_ids"] = row["evidence_root_ids"]
        if row["action"] == "reject":
            audit["rejection_reasons"] = [row["reason"]]
    safe_observe(observer, stage="decision_complete", batch_id=batch_id,
                 variant_key=variant_key, symbols=sorted(reports_by_symbol),
                 status="complete")
    completed_at = datetime.now(ZoneInfo("America/New_York"))
    cutoff = datetime.fromisoformat(evidence.manifest["scheduled_cutoff_et"])
    deadline = cutoff.replace(hour=9, minute=0, second=0, microsecond=0)
    result_unsigned = {
        "schema_version": 1,
        "completed_at_et": completed_at.isoformat(),
        "candidate_intake": evidence.candidate_intake,
        "score_eligible": evidence.manifest["cutoff_status"] == "on_time" and completed_at <= deadline,
        "variant": asdict(variant),
        "evidence_hash": evidence.manifest["evidence_hash"],
        "candidate_set_sha256": sha256_payload(evidence.candidate_intake),
        "model_name": profile.model,
        "model_profile_sha256": profile.identity(),
        "reports_by_symbol": {
            symbol: sorted(reports, key=lambda row: row["domain"])
            for symbol, reports in sorted(reports_by_symbol.items())
        },
        "adversary_by_symbol": dict(sorted(adversary_by_symbol.items())),
        "predictions": predictions,
        "decision": {
            "decisions": decision_result["decisions"],
            "audit": decision_result["audit"],
        },
        "integration_audit": audit_by_symbol,
        "model_call_audits": audits,
        "orders": [],
        "broker_modules_loaded": False,
        "research_mode": True,
        **({"synthesis": synthesis} if mode == "synthesis" else {}),
    }
    result = {
        **result_unsigned,
        "variant_result_sha256": sha256_payload(result_unsigned),
    }
    variant_root.mkdir(parents=True, exist_ok=True)
    _write_json_once(result_path, result)
    return result


class ResearchBatchRunner:
    def __init__(
        self,
        *,
        batches_root: Path,
        cache_root: Path,
        registry: LocalSkillRegistry,
        integration_policy: dict[str, Any],
    ) -> None:
        self.batches_root = batches_root
        self.cache = ContentAddressedModelCache(cache_root)
        self.registry = registry
        self.integration_policy = integration_policy

    def run(
        self,
        *,
        evidence: FrozenEvidence,
        variants: list[VariantSelection],
        profile: ModelProfile,
        secret: str,
        caller: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = call_structured,
        progress: Callable[[str, str], None] | None = None,
        observer: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        if not variants:
            raise ResearchBatchError("select at least one Skill version")
        unique = {variant.identity(): variant for variant in variants}
        if len(unique) != len(variants):
            raise ResearchBatchError("selected Skill versions contain duplicates")
        selected_commits = sorted(
            ({
                "author": variant.author,
                "version_id": variant.version_id,
                "source_commit_sha": variant.source_commit_sha,
                "version_sha256": variant.version_sha256,
            } for variant in variants),
            key=lambda row: (row["author"], row["version_id"], row["version_sha256"]),
        )
        ordered_variants = sorted(variants, key=lambda row: row.identity())
        skill_snapshots: dict[str, dict[str, Any]] = {}
        for variant in ordered_variants:
            key = f"{variant.author}/{variant.version_id}"
            documents = self.registry.effective_skills(
                variant.version_id, variant.author
            )
            unsigned_snapshot = {
                "schema_version": 1,
                "variant": asdict(variant),
                "documents": dict(sorted(documents.items())),
                "document_sha256": {
                    path: hashlib.sha256(content.encode("utf-8")).hexdigest()
                    for path, content in sorted(documents.items())
                },
            }
            skill_snapshots[key] = {
                **unsigned_snapshot,
                "skill_snapshot_sha256": sha256_payload(unsigned_snapshot),
            }
        skill_snapshot_sha256s = {
            key: value["skill_snapshot_sha256"]
            for key, value in sorted(skill_snapshots.items())
        }
        identity = {
            "evidence_hash": evidence.manifest["evidence_hash"],
            "candidate_set_hash": evidence.manifest["candidate_set_sha256"],
            "app_version": _application_version(),
            "operating_system": platform.system(),
            "data_provider_hash": evidence.manifest.get("provider_manifest", {}).get(
                "manifest_sha256"
            ),
            "model_profile_hash": profile.identity(),
            "selected_skill_commits": selected_commits,
            "skill_snapshot_sha256s": skill_snapshot_sha256s,
            "cutoff_status": evidence.manifest.get("cutoff_status"),
        }
        batch_id = "LAB-" + str(evidence.manifest["as_of_et"])[:10] + "-" + sha256_payload(identity)[:12]
        root = self.batches_root / batch_id
        root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(root / ".batch.lock"))
        with lock:
            for key, snapshot in sorted(skill_snapshots.items()):
                author, version_id = key.split("/", 1)
                _write_json_same_or_once(
                    root / "skills" / f"{_safe_name(author)}--{_safe_name(version_id)}.json",
                    snapshot,
                )
            manifest_path = root / "batch_manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("batch_identity_sha256") != sha256_payload(identity):
                    raise ResearchBatchError("batch directory belongs to another identity")
            else:
                manifest = {
                    "schema_version": 2, "batch_id": batch_id,
                    "batch_identity": identity,
                    "batch_identity_sha256": sha256_payload(identity),
                    "prediction_target": HORIZON,
                    "research_only": True,
                    "orders_allowed": False,
                    "skill_snapshot_sha256s": skill_snapshot_sha256s,
                }
                _write_json_once(manifest_path, manifest)
        results, failures = {}, {}

        def execute(variant: VariantSelection) -> tuple[str, dict[str, Any]]:
            key = f"{variant.author}/{variant.version_id}"
            if progress:
                progress(key, 'running')
            value = run_variant(
                variant=variant, evidence=evidence, registry=self.registry,
                profile=profile, secret=secret, cache=self.cache,
                integration_policy=self.integration_policy,
                output_root=root / "variants", caller=caller,
                observer=observer, batch_id=batch_id,
            )
            self.cache.snapshot_calls(
                value.get("model_call_audits", []),
                destination=root / "model_calls",
            )
            return key, value

        with ThreadPoolExecutor(
            max_workers=min(profile.max_concurrency, len(ordered_variants)),
            thread_name_prefix="shaq-shadow",
        ) as executor:
            future_to_variant = {
                executor.submit(execute, variant): variant for variant in ordered_variants
            }
            for future in as_completed(future_to_variant):
                variant = future_to_variant[future]
                key = f"{variant.author}/{variant.version_id}"
                try:
                    completed_key, value = future.result()
                    results[completed_key] = value
                    if progress:
                        progress(completed_key, 'complete')
                except Exception as exc:
                    safe_observe(observer, stage="failure", batch_id=batch_id,
                                 variant_key=key, status="failed",
                                 error_type=type(exc).__name__, message=str(exc))
                    failures[key] = {
                        "error_type": type(exc).__name__, "message": str(exc)
                    }
                    if progress:
                        progress(key, 'failed')
        status_unsigned = {
            "schema_version": 2, "batch_id": batch_id,
            "completed_variants": sorted(results), "failed_variants": failures,
            "all_variants_completed": not failures and len(results) == len(variants),
            "orders": [], "research_mode": True,
            "skill_snapshot_count": len(skill_snapshots),
            "model_call_document_count": len(list((root / "model_calls").glob("*.json"))),
        }
        status = {
            **status_unsigned,
            "batch_status_sha256": sha256_payload(status_unsigned),
        }
        previous_status = root / 'batch_status.json'
        if previous_status.exists():
            previous = json.loads(previous_status.read_text(encoding="utf-8"))
            _write_json_same_or_once(root / 'attempts' / (sha256_payload(previous) + '.json'), previous)
        _write_json_same_or_once(root / 'attempts' / (sha256_payload(status) + '.json'), status)
        _atomic_json(previous_status, status)
        return {"batch_root": str(root), "manifest": manifest, "status": status, "results": results}
