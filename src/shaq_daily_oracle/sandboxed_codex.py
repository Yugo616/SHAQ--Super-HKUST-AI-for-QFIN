from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import DOMAINS, validate_adversary_report, validate_domain_report
from .hashing import sha256_file, sha256_payload
from .isolation import IsolationError, validate_isolation_status


class SandboxedCodexError(ValueError):
    """The evidence-only Codex backend failed closed."""


DOMAIN_SKILLS = {
    "market": "market-common-shock",
    "relationships": "pit-peer-spillover",
    "event": "primary-event-reasoner",
    "capital": "capital-order-flow",
    "derivatives": "derivatives-evidence",
    "price_volume": "price-volume-structure",
}


def _seatbelt_quote(value: str | Path) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise SandboxedCodexError("sandbox paths cannot contain newlines")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(*, codex_path: Path, workspace_root: Path, temp_root: Path) -> str:
    """Return the minimal production profile used for every formal inference call."""

    return "\n".join((
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        f'(allow process-exec (literal "{_seatbelt_quote(codex_path)}"))',
        "(allow file-read*)",
        f'(deny file-read* (subpath "{_seatbelt_quote(workspace_root.resolve())}"))',
        f'(allow file-write* (subpath "{_seatbelt_quote(temp_root.resolve())}"))',
        "(allow network-outbound)",
        "(allow network-inbound)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow signal)",
        "(allow sysctl-read)",
        "",
    ))


def _discover_backend() -> tuple[Path, Path, Path]:
    if platform.system() != "Darwin":
        raise SandboxedCodexError("formal backend currently requires macOS Seatbelt")
    sandbox_exec = shutil.which("sandbox-exec")
    codex = shutil.which("codex")
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not sandbox_exec or not Path(sandbox_exec).is_file():
        raise SandboxedCodexError("sandbox-exec is unavailable")
    if not codex or not Path(codex).is_file():
        raise SandboxedCodexError("Codex CLI is unavailable")
    if not auth.is_file():
        raise SandboxedCodexError("Codex authentication is unavailable")
    return Path(sandbox_exec), Path(codex).resolve(), auth


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def attest_sandboxed_codex(*, workspace_root: Path, output: Path) -> dict[str, Any]:
    """Prove workspace denial and tool denial before enabling the formal path."""

    if output.exists():
        raise FileExistsError("isolation attestation is immutable")
    sandbox_exec, codex, _ = _discover_backend()
    started = datetime.now(ZoneInfo("America/New_York")).isoformat()
    with tempfile.TemporaryDirectory(prefix="shaq-daily-oracle-attest-") as name:
        temp_root = Path(name)
        production_profile = build_seatbelt_profile(
            codex_path=codex, workspace_root=workspace_root, temp_root=temp_root
        )
        profile_path = temp_root / "production.sb"
        profile_path.write_text(production_profile, encoding="utf-8")

        decoy = workspace_root / "shaq-daily-oracle" / ".isolation-decoy"
        created_decoy = not decoy.exists()
        if created_decoy:
            decoy.write_text("SHAQ_LABEL_DECOY_MUST_NOT_BE_READ", encoding="utf-8")
        try:
            read_profile = production_profile.replace(
                f'(allow process-exec (literal "{_seatbelt_quote(codex)}"))',
                '(allow process-exec (literal "/bin/cat"))',
            )
            read_profile_path = temp_root / "read-probe.sb"
            read_profile_path.write_text(read_profile, encoding="utf-8")
            denied = _run(
                [str(sandbox_exec), "-f", str(read_profile_path), "/bin/cat", str(decoy)],
                cwd=temp_root,
            )
            positive = _run(
                [str(sandbox_exec), "-f", str(read_profile_path), "/bin/cat", "/etc/hosts"],
                cwd=temp_root,
            )
        finally:
            if created_decoy:
                decoy.unlink(missing_ok=True)

        tool_probe = _run(
            [str(sandbox_exec), "-f", str(profile_path), "/bin/sh", "-c", "true"],
            cwd=temp_root,
        )
        version = _run([str(codex), "--version"], cwd=temp_root)
        checks = {
            "workspace_decoy_read_denied": denied.returncode != 0,
            "nonworkspace_control_read_allowed": positive.returncode == 0 and bool(positive.stdout),
            "non_codex_process_exec_denied": tool_probe.returncode != 0,
            "codex_version_available": version.returncode == 0 and bool(version.stdout.strip()),
        }
        enabled = all(checks.values())
        status = {
            "schema_version": 6,
            "backend": "macos-seatbelt-codex-cli" if enabled else "unavailable",
            "formal_ai_enabled": enabled,
            "evidence_read_only": enabled,
            "labels_unmounted": enabled,
            "network_denied": enabled,
            "tools_denied": enabled,
            "reason": (
                "kernel-enforced workspace denial; evidence is inlined by the parent; "
                "model-facing tools and external-data access are denied; transport to the "
                "Codex inference service remains allowed"
                if enabled else "one or more kernel isolation probes failed"
            ),
        }
        unsigned = {
            "schema_version": 6,
            "started_at_et": started,
            "completed_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "workspace_root": str(workspace_root.resolve()),
            "backend": status["backend"],
            "codex_cli_version": version.stdout.strip(),
            "production_profile_sha256": hashlib.sha256(production_profile.encode()).hexdigest(),
            "checks": checks,
            "status": status,
        }
        artifact = {**unsigned, "attestation_sha256": sha256_payload(unsigned)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not enabled:
        raise SandboxedCodexError("formal isolation attestation failed")
    return artifact


def verify_isolation_attestation(*, status: dict[str, Any], attestation_path: Path, workspace_root: Path) -> dict[str, Any]:
    normalized = validate_isolation_status(status)
    if normalized["formal_ai_enabled"] is not True:
        raise IsolationError("formal AI attestation cannot verify a disabled status")
    if not attestation_path.is_file():
        raise IsolationError("formal AI attestation is missing")
    artifact = json.loads(attestation_path.read_text(encoding="utf-8"))
    declared = artifact.get("attestation_sha256")
    unsigned = {key: value for key, value in artifact.items() if key != "attestation_sha256"}
    if declared != sha256_payload(unsigned):
        raise IsolationError("formal AI attestation hash mismatch")
    if artifact.get("status") != normalized:
        raise IsolationError("formal AI status differs from its attestation")
    if Path(str(artifact.get("workspace_root", ""))).resolve() != workspace_root.resolve():
        raise IsolationError("formal AI attestation belongs to another workspace")
    if not all(artifact.get("checks", {}).values()):
        raise IsolationError("formal AI attestation contains a failed probe")
    return artifact


def _report_schema() -> dict[str, Any]:
    report = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "domain", "as_of_et", "horizon", "availability", "verdict", "component_type", "thesis", "antithesis",
            "unknowns", "invalidation", "evidence_ids", "lineage_root_ids",
        ],
        "properties": {
            "domain": {"type": "string", "enum": sorted(DOMAINS)},
            "as_of_et": {"type": "string"},
            "horizon": {"type": "string", "const": "official_US_regular_session_open_to_close"},
            "availability": {"type": "string", "enum": ["available", "no_data", "not_entitled", "provider_error"]},
            "verdict": {"type": "string", "enum": ["bullish", "bearish", "neutral", "not_applicable", "unavailable"]},
            "component_type": {"type": "string", "enum": ["market_beta", "industry_spillover", "company_event", "capital_flow", "derivatives_distribution", "price_volume_state"]},
            "thesis": {"type": "string"},
            "antithesis": {"type": "string"},
            "unknowns": {"type": "array", "items": {"type": "string"}},
            "invalidation": {"type": "array", "items": {"type": "string"}},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "lineage_root_ids": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "report"],
                    "properties": {"task_id": {"type": "string"}, "report": report},
                },
            }
        },
    }


def _adversary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["symbol", "report"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "report": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "counts_as_vote", "new_evidence_allowed", "duplicate_lineage_roots",
                                "unresolved_conflicts", "strongest_countercase", "veto", "veto_reason",
                            ],
                            "properties": {
                                "counts_as_vote": {"type": "boolean", "const": False},
                                "new_evidence_allowed": {"type": "boolean", "const": False},
                                "duplicate_lineage_roots": {"type": "array", "items": {"type": "string"}},
                                "unresolved_conflicts": {"type": "array", "items": {"type": "string"}},
                                "strongest_countercase": {"type": "string"},
                                "veto": {"type": "boolean"},
                                "veto_reason": {"type": "string"},
                            },
                        },
                    },
                },
            }
        },
    }


def _resolve_evidence(path: str, evidence_root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = evidence_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(evidence_root.resolve())
    except ValueError as exc:
        raise SandboxedCodexError("evidence path escapes the frozen evidence root") from exc
    return resolved


def _inline_domain_packet(*, tasks: list[dict[str, Any]], evidence_root: Path, maximum_input_bytes: int) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    sanitized_tasks = []
    total = 0
    for task in sorted(tasks, key=lambda row: row["task_id"]):
        task_evidence = []
        for record in task["evidence"]:
            path = _resolve_evidence(record["raw_file_path"], evidence_root)
            if sha256_file(path) != record["raw_sha256"]:
                raise SandboxedCodexError("evidence changed after task construction")
            public = {
                key: value for key, value in record.items()
                if key not in {"raw_file_path", "analysis_file_path"}
            }
            task_evidence.append(public)
            if record["evidence_id"] not in evidence:
                content_path = path
                content_sha = record["raw_sha256"]
                content_field = "raw_content_utf8"
                if record.get("analysis_file_path"):
                    content_path = _resolve_evidence(record["analysis_file_path"], evidence_root)
                    content_sha = record.get("analysis_sha256")
                    content_field = "deterministic_analysis_view_utf8"
                content = content_path.read_bytes()
                if hashlib.sha256(content).hexdigest() != content_sha:
                    raise SandboxedCodexError("analysis evidence changed after task construction")
                total += len(content)
                if total > maximum_input_bytes:
                    raise SandboxedCodexError("domain evidence exceeds the governed input-size limit")
                evidence[record["evidence_id"]] = {
                    "metadata": public,
                    content_field: content.decode("utf-8", errors="replace"),
                }
        sanitized_tasks.append({
            "task_id": task["task_id"], "symbol": task["symbol"], "domain": task["domain"],
            "as_of_et": task["as_of_et"], "horizon": task["horizon"], "evidence": task_evidence,
            "collection_status": task.get("collection_status", {"status": "no_data", "details": []}),
        })
    return {"tasks": sanitized_tasks, "evidence": {key: evidence[key] for key in sorted(evidence)}}


def _codex_call(*, prompt: str, schema: dict[str, Any], config: dict[str, Any], workspace_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    sandbox_exec, codex, auth = _discover_backend()
    with tempfile.TemporaryDirectory(prefix="shaq-daily-oracle-infer-") as name:
        temp_root = Path(name)
        codex_home = temp_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        auth_copy = codex_home / "auth.json"
        shutil.copyfile(auth, auth_copy)
        auth_copy.chmod(0o600)
        profile = build_seatbelt_profile(
            codex_path=codex, workspace_root=workspace_root, temp_root=temp_root
        )
        profile_path = temp_root / "production.sb"
        schema_path = temp_root / "output-schema.json"
        output_path = temp_root / "last-message.json"
        profile_path.write_text(profile, encoding="utf-8")
        schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        command = [
            str(sandbox_exec), "-f", str(profile_path), str(codex), "exec", "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--sandbox", "read-only",
            "--model", str(config["model"]), "-c", f'model_reasoning_effort="{config["reasoning_effort"]}"',
            "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
        ]
        started = datetime.now(ZoneInfo("America/New_York")).isoformat()
        try:
            result = subprocess.run(
                command, cwd=temp_root, env=env, input=prompt, text=True, capture_output=True,
                timeout=int(config["timeout_seconds"]), check=False,
            )
            if result.returncode != 0 or not output_path.is_file():
                raise SandboxedCodexError(
                    "isolated Codex inference failed: " + (result.stderr[-1000:] or "no output")
                )
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            audit = {
                "started_at_et": started,
                "completed_at_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "model": config["model"],
                "reasoning_effort": config["reasoning_effort"],
                "codex_cli_version": _run([str(codex), "--version"], cwd=temp_root).stdout.strip(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "output_sha256": sha256_payload(parsed),
                "profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
                "transport_network_allowed": True,
                "model_facing_tools_allowed": False,
            }
            return parsed, audit
        finally:
            auth_copy.unlink(missing_ok=True)


def _load_config(config: dict[str, Any]) -> dict[str, Any]:
    required = ("model", "reasoning_effort", "timeout_seconds", "maximum_input_bytes")
    bindings = config.get("parameter_bindings", {})
    for name in required:
        if name not in config or len(bindings.get(name, [])) != 3:
            raise SandboxedCodexError(f"AI backend config lacks a governed {name}")
    if config["reasoning_effort"] not in {"low", "medium", "high", "xhigh"}:
        raise SandboxedCodexError("unsupported reasoning effort")
    if int(config["timeout_seconds"]) <= 0 or int(config["maximum_input_bytes"]) <= 0:
        raise SandboxedCodexError("AI backend resource limits must be positive")
    return config


def derive_predictions(*, reports_by_symbol: dict[str, list[dict[str, Any]]], adversary_by_symbol: dict[str, dict[str, Any]], candidate_intake: dict[str, Any], integration_policy: dict[str, Any]) -> list[dict[str, Any]]:
    minimum = int(integration_policy["minimum_aligned_independent_roots"])
    minimum_domains = int(integration_policy.get("minimum_aligned_applicable_domains", 2))
    maximum_opposed = int(integration_policy["maximum_opposed_independent_roots"])
    cap = int(integration_policy["maximum_predictions"])
    modern_policy = bool(integration_policy.get("root_component_types"))
    context_types = set(integration_policy.get("market_or_industry_root_component_types", ["market_context", "industry_context"]))
    stock_types = set(integration_policy.get("stock_specific_root_component_types", ["stock_event", "stock_capital", "stock_derivatives", "stock_price_volume"]))
    root_components = {
        key: set(value) for key, value in integration_policy.get("root_component_types", {}).items()
    }
    candidates = {row["symbol"]: row for row in candidate_intake.get("candidates", [])}
    eligible: list[tuple[int, str, str]] = []
    for symbol in sorted(reports_by_symbol):
        if adversary_by_symbol[symbol]["veto"]:
            continue
        direction_results = []
        for direction, opposite in (("bullish", "bearish"), ("bearish", "bullish")):
            aligned: set[str] = set()
            opposed: set[str] = set()
            aligned_by_domain: dict[str, set[str]] = {}
            for report in reports_by_symbol[symbol]:
                roots = set(report["lineage_root_ids"])
                if report["verdict"] == direction:
                    aligned.update(roots)
                    aligned_by_domain[report["domain"]] = roots
                elif report["verdict"] == opposite:
                    opposed.update(roots)
            conflict = aligned & opposed
            clean_aligned, clean_opposed = aligned - conflict, opposed - conflict
            aligned_domains = {
                domain for domain, roots in aligned_by_domain.items() if roots & clean_aligned
            }
            context_roots = {root for root in clean_aligned if root_components.get(root, set()) & context_types}
            stock_roots = {root for root in clean_aligned if root_components.get(root, set()) & stock_types}
            legacy_groups = [set(group) for group in integration_policy.get("required_aligned_domain_groups", [])]
            if (
                len(clean_aligned) >= minimum
                and len(aligned_domains) >= minimum_domains
                and len(clean_opposed) <= maximum_opposed
                and ((context_roots and stock_roots) if modern_policy else all(aligned_domains & group for group in legacy_groups))
            ):
                direction_results.append((len(clean_aligned), direction))
        if len(direction_results) == 1:
            root_count, direction = direction_results[0]
            eligible.append((-root_count, symbol, direction))
    predictions = []
    for _, symbol, direction in sorted(eligible)[:cap]:
        row = candidates[symbol]
        predictions.append({
            "symbol": symbol,
            "direction": direction,
            "track": "event" if row.get("captured_primary_event") is True else "ordinary",
            "score_eligible": True,
            "industry_group": row["gics_sector"],
        })
    return predictions


def integration_audit(
    *, reports_by_symbol: dict[str, list[dict[str, Any]]],
    adversary_by_symbol: dict[str, dict[str, Any]], predictions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    published = {row["symbol"] for row in predictions}
    output = {}
    for symbol, reports in sorted(reports_by_symbol.items()):
        applicable = [
            row for row in reports
            if row.get("availability") == "available" and row.get("verdict") != "not_applicable"
        ]
        directional = [row for row in applicable if row.get("verdict") in {"bullish", "bearish"}]
        directions = {row["verdict"] for row in directional}
        reasons = []
        if adversary_by_symbol[symbol].get("veto"):
            reasons.append("adversary_integrity_veto")
        if len(directional) < 2:
            reasons.append("fewer_than_two_directional_domains")
        if len(directions) > 1:
            reasons.append("independent_direction_conflict")
        if symbol not in published and not reasons:
            reasons.append("lineage_root_role_gate_not_met")
        output[symbol] = {
            "applicable_domain_count": len(applicable),
            "directional_domain_count": len(directional),
            "directional_domains": sorted(row["domain"] for row in directional),
            "published": symbol in published,
            "rejection_reasons": [] if symbol in published else reasons,
        }
    return output


def _bind_verified_lineage(
    report: dict[str, Any], evidence_to_roots: dict[str, Any]
) -> dict[str, Any]:
    """Derive roots from cited evidence; the model never authors DAG identity."""

    normalized = dict(report)
    roots: set[str] = set()
    for evidence_id in normalized.get("evidence_ids", []):
        if evidence_id not in evidence_to_roots:
            raise SandboxedCodexError(f"domain inference cited unknown evidence: {evidence_id}")
        values = evidence_to_roots[evidence_id]
        roots.update([values] if isinstance(values, str) else values)
    normalized["lineage_root_ids"] = sorted(roots)
    return normalized


def run_sandboxed_six_domain(*, run_id: str, created_at: str, cutoff_et: str, tasks_document: dict[str, Any], lineage: dict[str, Any], candidate_intake: dict[str, Any], evidence_manifest: dict[str, Any], evidence_root: Path, package_root: Path, workspace_root: Path, isolation_status: dict[str, Any], attestation_path: Path, config: dict[str, Any], integration_policy: dict[str, Any], calls_dir: Path) -> dict[str, Any]:
    verify_isolation_attestation(
        status=isolation_status, attestation_path=attestation_path, workspace_root=workspace_root
    )
    config = _load_config(config)
    evidence_to_roots = lineage["evidence_to_roots"]
    evidence_domains = {
        row["evidence_id"]: set(row.get("consumer_domains", []))
        or {task["domain"] for task in tasks_document.get("tasks", []) if any(
            evidence.get("evidence_id") == row["evidence_id"] for evidence in task.get("evidence", [])
        )}
        for row in lineage["records"]
    }
    tasks = tasks_document.get("tasks", [])
    reports_by_symbol: dict[str, list[dict[str, Any]]] = {
        row["symbol"]: [] for row in candidate_intake.get("candidates", [])
    }
    calls_dir.mkdir(parents=True, exist_ok=True)
    def analyze_domain(domain: str) -> list[tuple[str, dict[str, Any]]]:
        domain_tasks = [row for row in tasks if row.get("domain") == domain]
        if {row["symbol"] for row in domain_tasks} != set(reports_by_symbol):
            raise SandboxedCodexError(f"{domain} tasks do not match candidate intake")
        packet = _inline_domain_packet(
            tasks=domain_tasks, evidence_root=evidence_root,
            maximum_input_bytes=int(config["maximum_input_bytes"]),
        )
        skill_dir = package_root / "skills" / DOMAIN_SKILLS[domain]
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        foundations = (skill_dir / "references" / "foundations.md").read_text(encoding="utf-8")
        prompt = (
            "You are the isolated SHAQ Daily Oracle domain analyst. Follow the supplied Skill exactly. "
            "Use only the inlined packet. You have no permission to browse, use remembered company facts, "
            "infer missing measurements, or access files/tools. Analyze each task independently. Preserve the "
            "task as_of_et and horizon exactly. Cite only that task's evidence IDs; the deterministic program "
            "will derive lineage roots from those IDs and ignore model-authored root identity. "
            "Map collector status exactly: not_applicable means availability=available and verdict=not_applicable; "
            "a usable but nondirectional input means availability=available and verdict=neutral; no_data, "
            "not_entitled or provider_error require the matching availability and verdict=unavailable. "
            "A directional verdict requires evidence satisfying the Skill. "
            "Do not report probability, confidence, strength, score, ranking, labels, outcomes, or another domain.\n\n"
            f"SKILL:\n{skill}\n\nSELECTED FOUNDATIONS:\n{foundations}\n\n"
            f"FROZEN PACKET JSON:\n{json.dumps(packet, sort_keys=True, ensure_ascii=False)}"
        )
        call_path = calls_dir / f"{domain}.json"
        prompt_path = calls_dir / f"{domain}.prompt"
        packet_path = calls_dir / f"{domain}.packet"
        existing = [path.exists() for path in (call_path, prompt_path, packet_path)]
        if any(existing) and not all(existing):
            raise SandboxedCodexError(f"partial immutable {domain} call artifact")
        if all(existing):
            saved = json.loads(call_path.read_text(encoding="utf-8"))
            if (
                prompt_path.read_text(encoding="utf-8") != prompt
                or json.loads(packet_path.read_text(encoding="utf-8")) != packet
                or saved.get("input_sha256") != sha256_payload(packet)
                or saved.get("backend_config_sha256") != sha256_payload(config)
                or saved.get("backend_config") != config
                or saved.get("prompt_file_sha256") != sha256_file(prompt_path)
                or saved.get("packet_file_sha256") != sha256_file(packet_path)
            ):
                raise SandboxedCodexError(f"immutable {domain} call belongs to different inputs")
            parsed, audit = saved.get("raw_output"), saved.get("audit")
        else:
            saved = None
            parsed, audit = _codex_call(
                prompt=prompt, schema=_report_schema(), config=config, workspace_root=workspace_root
            )
        if (
            not isinstance(parsed, dict)
            or not isinstance(audit, dict)
            or sha256_payload(parsed) != audit.get("output_sha256")
            or audit.get("model") != config["model"]
            or audit.get("reasoning_effort") != config["reasoning_effort"]
        ):
            raise SandboxedCodexError(f"{domain} inference audit is invalid")
        expected = {row["task_id"]: row for row in domain_tasks}
        results = parsed.get("results", [])
        if len(results) != len(expected) or {row.get("task_id") for row in results} != set(expected):
            raise SandboxedCodexError(f"{domain} inference did not return each task exactly once")
        normalized_results = []
        for item in results:
            task = expected[item["task_id"]]
            report = item["report"]
            if (
                report.get("domain") != domain
                or report.get("as_of_et") != task["as_of_et"]
                or report.get("horizon") != task["horizon"]
            ):
                raise SandboxedCodexError("domain inference changed its task identity")
            allowed_ids = {row["evidence_id"] for row in task["evidence"]}
            if not set(report.get("evidence_ids", [])).issubset(allowed_ids):
                raise SandboxedCodexError("domain inference cited evidence outside its blind task")
            report = _bind_verified_lineage(report, evidence_to_roots)
            normalized = validate_domain_report(report, evidence_to_roots, evidence_domains)
            normalized_results.append({"task_id": item["task_id"], "report": normalized})
        reports = [
            (expected[item["task_id"]]["symbol"], item["report"])
            for item in normalized_results
        ]
        call_artifact = {
            "schema_version": 6, "domain": domain,
            "input_sha256": sha256_payload(packet), "audit": audit,
            "backend_config": config,
            "backend_config_sha256": sha256_payload(config),
            "raw_output": parsed,
            "results": sorted(normalized_results, key=lambda row: row["task_id"]),
        }
        if saved is not None:
            if saved.get("results") != call_artifact["results"]:
                raise SandboxedCodexError(f"immutable {domain} normalized result changed")
        else:
            prompt_path.write_text(prompt, encoding="utf-8")
            packet_path.write_text(json.dumps(packet, sort_keys=True, ensure_ascii=False), encoding="utf-8")
            call_artifact["prompt_file"] = prompt_path.name
            call_artifact["prompt_file_sha256"] = sha256_file(prompt_path)
            call_artifact["packet_file"] = packet_path.name
            call_artifact["packet_file_sha256"] = sha256_file(packet_path)
            call_path.write_text(json.dumps(call_artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return reports

    with ThreadPoolExecutor(max_workers=len(DOMAINS), thread_name_prefix="daily-oracle-domain") as pool:
        futures = {domain: pool.submit(analyze_domain, domain) for domain in sorted(DOMAINS)}
        domain_outputs = {domain: futures[domain].result() for domain in sorted(futures)}
    for domain in sorted(domain_outputs):
        for symbol, report in sorted(domain_outputs[domain], key=lambda item: item[0]):
            reports_by_symbol[symbol].append(report)

    adversary_skill = (package_root / "skills" / "thesis-adversary" / "SKILL.md").read_text(encoding="utf-8")
    adversary_foundations = (package_root / "skills" / "thesis-adversary" / "references" / "foundations.md").read_text(encoding="utf-8")
    adversary_packet = {
        "reports_by_symbol": {key: sorted(value, key=lambda row: row["domain"]) for key, value in sorted(reports_by_symbol.items())},
        "verified_lineage_roots": sorted(lineage["root_component_types"]),
    }
    adversary_prompt = (
        "You are the isolated non-voting SHAQ Daily Oracle adversary. Follow the supplied Skill. "
        "Use only the completed reports and verified root list below. Do not add facts or evidence. "
        "Return exactly one review per symbol. A veto is only for the Skill's enumerated integrity failures, "
        "not merely weak or conflicting investment evidence.\n\n"
        f"SKILL:\n{adversary_skill}\n\nSELECTED FOUNDATIONS:\n{adversary_foundations}\n\n"
        f"PACKET JSON:\n{json.dumps(adversary_packet, sort_keys=True, ensure_ascii=False)}"
    )
    adversary_path = calls_dir / "adversary.json"
    adversary_prompt_path = calls_dir / "adversary.prompt"
    adversary_packet_path = calls_dir / "adversary.packet"
    adversary_existing = [
        path.exists() for path in (adversary_path, adversary_prompt_path, adversary_packet_path)
    ]
    if any(adversary_existing) and not all(adversary_existing):
        raise SandboxedCodexError("partial immutable adversary call artifact")
    if all(adversary_existing):
        saved_adversary = json.loads(adversary_path.read_text(encoding="utf-8"))
        if (
            adversary_prompt_path.read_text(encoding="utf-8") != adversary_prompt
            or json.loads(adversary_packet_path.read_text(encoding="utf-8")) != adversary_packet
            or saved_adversary.get("input_sha256") != sha256_payload(adversary_packet)
            or saved_adversary.get("backend_config_sha256") != sha256_payload(config)
            or saved_adversary.get("backend_config") != config
            or saved_adversary.get("prompt_file_sha256") != sha256_file(adversary_prompt_path)
            or saved_adversary.get("packet_file_sha256") != sha256_file(adversary_packet_path)
        ):
            raise SandboxedCodexError("immutable adversary call belongs to different inputs")
        parsed, audit = saved_adversary.get("raw_output"), saved_adversary.get("audit")
    else:
        saved_adversary = None
        parsed, audit = _codex_call(
            prompt=adversary_prompt, schema=_adversary_schema(), config=config, workspace_root=workspace_root
        )
    if (
        not isinstance(parsed, dict)
        or not isinstance(audit, dict)
        or sha256_payload(parsed) != audit.get("output_sha256")
        or audit.get("model") != config["model"]
        or audit.get("reasoning_effort") != config["reasoning_effort"]
    ):
        raise SandboxedCodexError("adversary inference audit is invalid")
    results = parsed.get("results", [])
    if len(results) != len(reports_by_symbol) or {row.get("symbol") for row in results} != set(reports_by_symbol):
        raise SandboxedCodexError("adversary did not return each candidate exactly once")
    adversary_by_symbol = {}
    known_roots = set(lineage["root_component_types"])
    for item in results:
        report = validate_adversary_report(item["report"])
        if not set(report["duplicate_lineage_roots"]).issubset(known_roots):
            raise SandboxedCodexError("adversary cited an unknown lineage root")
        adversary_by_symbol[item["symbol"]] = report
    adversary_artifact = {
        "schema_version": 6, "input_sha256": sha256_payload(adversary_packet),
        "audit": audit, "backend_config": config,
        "backend_config_sha256": sha256_payload(config), "raw_output": parsed,
        "results": [{"symbol": symbol, "report": adversary_by_symbol[symbol]} for symbol in sorted(adversary_by_symbol)],
    }
    if saved_adversary is not None:
        if saved_adversary.get("results") != adversary_artifact["results"]:
            raise SandboxedCodexError("immutable adversary normalized result changed")
    else:
        adversary_prompt_path.write_text(adversary_prompt, encoding="utf-8")
        adversary_packet_path.write_text(
            json.dumps(adversary_packet, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        adversary_artifact["prompt_file"] = adversary_prompt_path.name
        adversary_artifact["prompt_file_sha256"] = sha256_file(adversary_prompt_path)
        adversary_artifact["packet_file"] = adversary_packet_path.name
        adversary_artifact["packet_file_sha256"] = sha256_file(adversary_packet_path)
        adversary_path.write_text(json.dumps(adversary_artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    governed_policy = {
        **integration_policy,
        "root_component_types": lineage["root_component_types"],
    }
    predictions = derive_predictions(
        reports_by_symbol=reports_by_symbol, adversary_by_symbol=adversary_by_symbol,
        candidate_intake=candidate_intake, integration_policy=governed_policy,
    )
    legacy_shadow_predictions = derive_predictions(
        reports_by_symbol=reports_by_symbol, adversary_by_symbol=adversary_by_symbol,
        candidate_intake=candidate_intake,
        integration_policy={
            "minimum_aligned_independent_roots": int(integration_policy["minimum_aligned_independent_roots"]),
            "maximum_opposed_independent_roots": int(integration_policy["maximum_opposed_independent_roots"]),
            "maximum_predictions": int(integration_policy["maximum_predictions"]),
            "required_aligned_domain_groups": [
                ["market", "relationships", "event"],
                ["capital", "derivatives", "price_volume"],
            ],
        },
    )
    return {
        "run_id": run_id,
        "created_at": created_at,
        "cutoff_et": cutoff_et,
        "evidence": evidence_manifest.get("evidence", []),
        "reports_by_symbol": {key: sorted(value, key=lambda row: row["domain"]) for key, value in sorted(reports_by_symbol.items())},
        "adversary_by_symbol": {key: adversary_by_symbol[key] for key in sorted(adversary_by_symbol)},
        "predictions": predictions,
        "legacy_gate_shadow_predictions": legacy_shadow_predictions,
        "integration_audit_by_symbol": integration_audit(
            reports_by_symbol=reports_by_symbol, adversary_by_symbol=adversary_by_symbol,
            predictions=predictions,
        ),
    }
