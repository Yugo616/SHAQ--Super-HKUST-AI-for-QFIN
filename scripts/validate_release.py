#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".css", ".html", ".iss", ".js", ".json", ".md", ".ps1", ".py",
    ".sh", ".toml", ".yaml", ".yml",
}
IGNORED_TOP_LEVEL = {
    ".git", ".pytest_cache", "build", "dist", "runtime",
}
DISALLOWED_TEXT = ("to" + "do", "pend" + "ing")
DISALLOWED_PATH_PATTERNS = (
    re.compile("/" + "Users/" + "[^/]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\]+\\"),
    re.compile(r"\bhybrid_" + r"qlib_demo\b"),
)
REQUIRED_CONFIG_BINDINGS = {
    "ai-backend.json": {
        "backend", "model", "reasoning_effort", "timeout_seconds",
        "maximum_input_bytes", "maximum_output_tokens",
    },
    "canary.example.json": {
        "forecast_cutoff", "entry_after", "entry_deadline", "exit_at", "exit_deadline",
        "order_poll_interval_seconds",
        "trd_env", "real_trading_enabled", "account_allowlist", "max_forecasts",
        "shares_per_forecast", "max_portfolio_age_seconds", "max_borrow_age_seconds",
    },
    "candidate-intake.json": {
        "maximum_price_residual_candidates", "maximum_captured_event_candidates",
        "minimum_premarket_volume_quantile", "maximum_snapshot_skew_seconds",
    },
    "event-analysis.json": {"document_types", "maximum_output_bytes"},
    "event-discovery.json": {
        "sec_forms", "sec_feed_page_size", "sec_feed_max_pages_per_form",
        "sec_feed_maximum_bytes", "sec_document_maximum_bytes",
        "sec_retry_attempts", "sec_retry_backoff_seconds",
    },
    "integration.json": {
        "minimum_aligned_independent_roots", "maximum_opposed_independent_roots",
        "maximum_predictions", "minimum_aligned_applicable_domains",
        "market_or_industry_root_component_types", "stock_specific_root_component_types",
    },
    "market-data.json": {
        "provider", "batch_size", "minimum_coverage", "minimum_semantic_pass_rate",
        "premarket_return_tolerance", "provider_market_prefix",
        "class_share_input_separator", "class_share_provider_separator",
    },
    "price-history.json": {"lookback_calendar_days", "minimum_bars", "maximum_symbols", "max_count"},
    "deep-evidence.json": {
        "order_book_depth", "order_book_samples", "sample_interval_seconds",
        "ticker_max_count", "capital_window_start", "relationship_exposure_window", "option_expiry_min_days",
        "option_expiry_max_days", "option_max_contracts", "option_event_page_count",
        "capital_capture_not_before", "capital_time_segments",
    },
    "runtime.json": {
        "timezone", "precheck_start", "evidence_cutoff", "forecast_deadline",
        "entry_after", "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after", "order_poll_interval_seconds", "account_alias",
        "shares_per_forecast", "maximum_forecasts",
    },
    "postmortem.json": {
        "provisional_capture_after_et", "final_reobservation_after_et",
        "market_symbol", "batch_review_interval_trading_days",
        "first_batch_review_date", "batch_review_after_et", "drift_alert_delta",
        "automatic_production_mutation_allowed", "approved_reference_ids",
        "postmortem_pipeline_identity",
    },
    "reliability.json": {
        "certification_repetitions", "daily_health_check_times_et",
        "health_model_timeout_seconds", "minimum_free_gib", "notification_title",
    },
}


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) > 150:
        raise ValueError(f"skill exceeds 150 lines: {path}")
    if not lines or lines[0] != "---" or lines.count("---") < 2:
        raise ValueError(f"invalid frontmatter: {path}")
    closing = lines[1:].index("---") + 1
    frontmatter = lines[1:closing]
    keys = {line.split(":", 1)[0] for line in frontmatter if ":" in line}
    if keys != {"name", "description"}:
        raise ValueError(f"frontmatter keys must be name and description: {path}")
    if "references/foundations.md" not in text:
        raise ValueError(f"skill does not route to foundations: {path}")
    metadata = path.parent / "agents" / "openai.yaml"
    if not metadata.is_file():
        raise ValueError(f"skill lacks agents/openai.yaml: {path}")
    metadata_text = metadata.read_text(encoding="utf-8")
    skill_name = path.parent.name
    if f"${skill_name}" not in metadata_text:
        raise ValueError(f"skill UI metadata lacks a named default prompt: {metadata}")


def main() -> int:
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    if len(skills) != 8:
        raise ValueError("release requires one orchestrator, six domains and one adversary")
    for skill in skills:
        validate_skill(skill)
    implicit = {}
    for skill in skills:
        metadata = (skill.parent / "agents/openai.yaml").read_text(encoding="utf-8")
        match = re.search(r"allow_implicit_invocation:\s*(true|false)", metadata)
        if not match:
            raise ValueError(f"skill lacks an implicit-invocation policy: {skill}")
        implicit[skill.parent.name] = match.group(1) == "true"
    if implicit != {name: name == "daily-oracle" for name in implicit}:
        raise ValueError("only the daily-oracle orchestrator may be invoked implicitly")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(ROOT)
        if relative.parts[0] in IGNORED_TOP_LEVEL or any(
            part == "__pycache__" or part.endswith(".egg-info") for part in relative.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            ast.parse(text, filename=str(path))
        comparable = text.lower()
        for marker in DISALLOWED_TEXT:
            if marker in comparable:
                raise ValueError(f"internal marker in public package: {path}")
        sensitive_markers = ("REAL" + "_" + "ENABLED",)
        if any(marker in text for marker in sensitive_markers):
            raise ValueError(f"credential or real-trading switch in release: {path}")
        if any(pattern.search(text) for pattern in DISALLOWED_PATH_PATTERNS):
            raise ValueError(f"local or legacy path in public package: {path}")
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("skills") != "./skills/":
        raise ValueError("plugin manifest does not expose skills")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'daily-oracle = "shaq_daily_oracle.cli:main"' not in project:
        raise ValueError("the public package lacks its single executable entrypoint")
    registries = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "governance").glob("*registry.json"))
    ]
    known = {
        "REF": {key for registry in registries for key in registry.get("references", {})},
        "DEC": {key for registry in registries for key in registry.get("decisions", {})},
        "EXP": {key for registry in registries for key in registry.get("experiments", {})},
    }
    for config_path in sorted((ROOT / "config").glob("*.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config_path.name in REQUIRED_CONFIG_BINDINGS:
            bindings = config.get("parameter_bindings", {})
            if set(bindings) != REQUIRED_CONFIG_BINDINGS[config_path.name]:
                raise ValueError(f"formal parameter binding coverage differs in {config_path}")
        elif config_path.name == "readiness.json":
            for section_name in ("probability", "cost", "net_profit"):
                section = config.get(section_name, {})
                parameters = set(section) - {
                    "reference_id", "decision_id", "experiment_id", "parameter_bindings"
                }
                if set(section.get("parameter_bindings", {})) != parameters:
                    raise ValueError(
                        f"formal parameter binding coverage differs in {config_path}:{section_name}"
                    )
        stack = [config]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                if (
                    len(value) == 3
                    and all(isinstance(item, str) for item in value)
                    and value[0].startswith("REF-")
                    and value[1].startswith("DEC-")
                    and value[2].startswith("EXP-")
                ):
                    for prefix, identifier in zip(("REF", "DEC", "EXP"), value, strict=True):
                        if identifier not in known[prefix]:
                            raise ValueError(f"unregistered {prefix} binding in {config_path}: {identifier}")
                else:
                    stack.extend(value)
    core_manifest = json.loads(
        (ROOT / "governance/formal-core-manifest.json").read_text(encoding="utf-8")
    )
    if core_manifest.get("schema_version") != 1 or not core_manifest.get("include_patterns"):
        raise ValueError("formal-core manifest is incomplete")
    for prefix, key in (
        ("REF", "reference_id"), ("DEC", "decision_id"), ("EXP", "experiment_id")
    ):
        if core_manifest.get(key) not in known[prefix]:
            raise ValueError(f"formal-core manifest has an unknown {key}")
    print(f"release validation passed: {len(skills)} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
