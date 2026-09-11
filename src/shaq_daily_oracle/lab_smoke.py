"""Deterministic, broker-free whole-Lab fixture for source and packaged smoke checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .bundled_versions import install_bundled_versions
from .hashing import sha256_payload
from .lab_service import SKILL_EXPLANATIONS
from .market_calendar import market_session
from .minute_settlements import MINUTE_NAMESPACE, MinuteStore
from .model_backends import ModelProfile
from .research_batch import (
    ResearchBatchRunner, VariantSelection, freeze_evidence_bundle, load_frozen_evidence,
)
from .research_dashboard import ResearchDashboardIndex
from .settings import _atomic_json
from .skill_versions import LocalSkillRegistry
from .virtual_accounts import AccountRules, AccountStore, replay_day


TRADE_DATE = "2026-09-09"


def _fixture_evidence(root: Path):
    as_of = f"{TRADE_DATE}T08:40:00-04:00"
    return freeze_evidence_bundle(
        root=root,
        as_of_et=as_of,
        scheduled_cutoff_et=f"{TRADE_DATE}T08:50:00-04:00",
        cutoff_status="on_time",
        candidates=[{
            "symbol": "AAPL", "gics_sector": "Information Technology",
            "captured_primary_event": True, "selection_method": "fixture_frozen_candidate",
            "premarket_return": 0.01, "sector_premarket_return": 0.002,
        }],
        records=[
            {
                "evidence_id": "ev_market_fixture", "domain": "market",
                "provider": "deterministic-fixture", "source_uri": "fixture://market",
                "captured_at": as_of, "raw_file_path": "raw/market.json",
                "scope_symbols": ["*"],
                "consumer_domains": ["market", "price_volume"],
                "root_component_type": "market_context",
            },
            {
                "evidence_id": "ev_price_fixture", "domain": "price_volume",
                "provider": "deterministic-fixture", "source_uri": "fixture://aapl",
                "captured_at": as_of, "raw_file_path": "raw/aapl.json",
                "scope_symbols": ["AAPL"],
                "consumer_domains": [
                    "relationships", "event", "capital", "derivatives", "price_volume"
                ],
                "root_component_type": "stock_price_volume",
            },
        ],
        files={
            "raw/market.json": b'{"SPY":{"return":0.01}}',
            "raw/aapl.json": b'{"symbol":"AAPL","premarket_gap":0.01}',
        },
        provider_manifest={
            "profile_id": "deterministic-fixture", "network_used": False,
            "provider_secrets_used": False,
        },
    )


class _DeterministicModel:
    """Complete structured fixture caller; it never opens a provider connection."""

    def __call__(self, *, profile, secret, prompt, schema):
        if secret:
            raise ValueError("the smoke fixture must not receive a provider credential")
        if "FROZEN TASKS:\n" in prompt:
            tasks = json.loads(prompt.split("FROZEN TASKS:\n", 1)[1])
            components = {
                "market": "market_beta", "relationships": "industry_spillover",
                "event": "company_event", "capital": "capital_flow",
                "derivatives": "derivatives_distribution",
                "price_volume": "price_volume_state",
            }
            results = []
            for task in tasks:
                evidence_ids = [
                    row["evidence_id"] for row in task["evidence"]
                    if row["evidence_id"].startswith(
                        "ev_market" if task["domain"] == "market" else "ev_price"
                    )
                ]
                results.append({
                    "task_id": task["task_id"],
                    "report": {
                        "domain": task["domain"], "as_of_et": task["as_of_et"],
                        "horizon": task["horizon"], "availability": "available",
                        "verdict": "bullish", "component_type": components[task["domain"]],
                        "thesis": "冻结夹具资料支持正向日内检验。",
                        "antithesis": "盘前变化也可能在开盘前被吸收。",
                        "unknowns": [], "invalidation": ["开盘后方向未延续"],
                        "evidence_ids": evidence_ids, "lineage_root_ids": [],
                    },
                })
            value = {"results": results}
        elif "FROZEN SYNTHESIS INPUT:\n" in prompt:
            packet = json.loads(prompt.split("FROZEN SYNTHESIS INPUT:\n", 1)[1])
            decisions = []
            for symbol, reports in sorted(packet["reports_by_symbol"].items()):
                evidence_ids = sorted({
                    evidence_id for report in reports
                    for evidence_id in report.get("evidence_ids", [])
                })
                decisions.append({
                    "symbol": symbol, "action": "publish", "direction": "bullish",
                    "thesis": "公司与市场资料共同支持正向观察。",
                    "antithesis": "最强反方是盘前已经充分定价。",
                    "resolution": "保留正向预测并以收盘结果检验。",
                    "comparison": "在同一冻结候选内，该机制更直接。",
                    "unknowns": ["盘中路径"], "invalidation": ["方向未延续"],
                    "evidence_ids": evidence_ids,
                })
            value = {"decisions": decisions}
        else:
            reports = json.loads(prompt.split("REPORTS:\n", 1)[1])
            value = {"results": [{
                "symbol": symbol,
                "report": {
                    "counts_as_vote": False, "new_evidence_allowed": False,
                    "duplicate_lineage_roots": [],
                    "unresolved_conflicts": [],
                    "strongest_countercase": "盘前变化可能已经被价格吸收。",
                    "veto": False, "veto_reason": "",
                },
            } for symbol in sorted(reports)]}
        audit = {
            "provider": "deterministic-model-fixture", "response_model": profile.model,
            "profile_sha256": profile.identity(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": sha256_payload(schema), "output_sha256": sha256_payload(value),
            "started_at_et": f"{TRADE_DATE}T08:41:00-04:00",
            "completed_at_et": f"{TRADE_DATE}T08:41:01-04:00",
        }
        return value, audit


def _minute_records(*, include_exit: bool = True) -> dict[str, list[dict[str, Any]]]:
    session = market_session(date.fromisoformat(TRADE_DATE))
    if session is None:
        raise ValueError("fixture date must be a market session")
    points = [(session.market_open + timedelta(minutes=1), 100.0)]
    if include_exit:
        points.append((session.market_close - timedelta(minutes=5), 102.0))
    return {"AAPL": [{
        "timestamp": stamp.isoformat(), "open": price, "high": price,
        "low": price, "close": price, "volume": 10_000,
    } for stamp, price in points]}


def _labels() -> dict[str, dict[str, Any]]:
    return {"AAPL": {
        "status": "final", "official_unadjusted_open": 100.0,
        "official_unadjusted_close": 101.0, "actual_direction": "bullish",
    }}


def _signed_labels() -> dict[str, Any]:
    unsigned = {"schema_version": 1, "labels": _labels()}
    return {**unsigned, "labels_sha256": sha256_payload(unsigned)}


def _account_rows(
    results: dict[str, dict[str, Any]], minute: dict[str, Any], *, batch_id: str
):
    rows = []
    for key, variant in sorted(results.items()):
        rows.append({
            "batch_id": batch_id, "variant_key": key,
            "series_key": f"{key}:{variant['variant']['version_sha256']}:{variant['model_profile_sha256']}",
            "label": variant["variant"]["label"], "model": variant["model_name"],
            "method_identity": variant["variant"]["version_sha256"],
            "model_identity": variant["model_profile_sha256"],
            "trade_date": TRADE_DATE, "completed_at_et": f"{TRADE_DATE}T08:42:00-04:00",
            "publication_deadline_et": f"{TRADE_DATE}T09:00:00-04:00",
            "source_eligible": True, "variant_result_sha256": variant["variant_result_sha256"],
            "labels": _labels(), "minute": minute,
            "predictions": [
                {"symbol": row["symbol"], "direction": row["direction"]}
                for row in variant["predictions"]
            ],
        })
    return rows


def _contract_trade(result: dict[str, Any]) -> dict[str, Any]:
    trade = result["trades"][0]
    return {
        key: trade[key] for key in (
            "direction", "quantity", "entry_reference_open", "exit_reference_open",
            "entry_price", "exit_price", "gross_pnl", "fees", "slippage_cost",
            "net_pnl",
        )
    } | {
        "reserved_collateral": result["opening_capital_used"],
        "closing_cash": result["closing_cash"],
        "reconciled": (
            not result["closing_positions"]
            and result["zero_cost"]["reconciled"] is True
            and math.isclose(
                result["net_pnl"],
                result["gross_pnl"] - result["fees"] - result["slippage_cost"],
                abs_tol=1e-9,
            )
            and math.isclose(
                result["closing_cash"],
                result["opening_cash"] + result["net_pnl"],
                abs_tol=1e-9,
            )
        ),
    }


def run_lab_smoke(*, package_root: Path, output_root: Path) -> dict[str, Any]:
    """Run the two shipped methods and genuine Zipline minute ledger in isolation."""
    package_root = package_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    research_root = output_root / "research"
    registry = LocalSkillRegistry(
        root=research_root / "skill_versions", package_skills=package_root / "skills"
    )
    install_bundled_versions(registry)
    methods = registry.list_method_versions()
    variants = [VariantSelection.from_registry_row(row) for row in methods]
    staged_evidence = _fixture_evidence(research_root / "staged-evidence")
    evidence_root = research_root / "evidence" / staged_evidence.manifest["evidence_hash"]
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    if not evidence_root.exists():
        staged_evidence.root.replace(evidence_root)
    evidence = load_frozen_evidence(evidence_root)
    profile = ModelProfile(
        profile_id="deterministic-fixture", protocol="openai-chat-completions",
        base_url="https://fixture.invalid/v1", model="deterministic-model-fixture",
        output_mode="strict", max_concurrency=1,
    )
    runner = ResearchBatchRunner(
        batches_root=research_root / "batches", cache_root=research_root / "model-cache",
        registry=registry,
        integration_policy=json.loads(
            (package_root / "config/integration.json").read_text(encoding="utf-8")
        ),
    )
    from .research_progress import ResearchProgressLog
    progress = ResearchProgressLog(output_root / "research-progress.jsonl")
    batch = runner.run(
        evidence=evidence, variants=variants, profile=profile, secret="",
        caller=_DeterministicModel(), observer=progress.append,
    )
    batch_root = Path(batch["batch_root"])
    _atomic_json(batch_root / "labels.json", _signed_labels())
    dashboard = ResearchDashboardIndex(
        batches_root=research_root / "batches", database=research_root / "index.sqlite3"
    )
    detail = dashboard.batch_detail(batch["status"]["batch_id"])
    batch_id = detail["batch_id"]

    minute_store = MinuteStore(research_root / MINUTE_NAMESPACE)
    provisional_minute = minute_store.observe(
        TRADE_DATE, ["AAPL"], _minute_records(), provider="yfinance",
        observed_at=f"{TRADE_DATE}T16:10:00-04:00",
    )
    accounts = AccountStore(research_root / "virtual_accounts")
    accounts.activate(AccountRules(), f"{TRADE_DATE}T07:00:00-04:00")
    provisional_rows = _account_rows(
        batch["results"], provisional_minute, batch_id=batch_id
    )
    provisional = accounts.refresh(provisional_rows)
    final_minute = minute_store.observe(
        TRADE_DATE, ["AAPL"], _minute_records(), provider="yfinance",
        observed_at=f"{TRADE_DATE}T16:20:00-04:00",
    )
    final_rows = _account_rows(batch["results"], final_minute, batch_id=batch_id)
    final = accounts.refresh(final_rows)
    reopened = AccountStore(research_root / "virtual_accounts").view(final_rows)
    detail["virtual_accounts"] = {
        **final, "results": [
            row for row in final["results"] if row["variant_key"] in detail["variants"]
        ],
    }

    predictions = final_rows[0]["predictions"]
    rules = AccountRules()
    empty = replay_day(TRADE_DATE, [], {}, rules)
    pending = replay_day(TRADE_DATE, predictions, _labels(), rules, minute={})
    incomplete_minute = {
        "status": "provisional", "records": _minute_records(include_exit=False),
        "observation_hashes": ["fixture-incomplete"], "execution_sha256": "fixture-incomplete",
        "captured_at_et": f"{TRADE_DATE}T16:10:00-04:00",
    }
    incomplete = replay_day(
        TRADE_DATE, predictions, _labels(), rules, minute=incomplete_minute
    )
    error_store = AccountStore(research_root / "error-account")
    error_store.activate(rules, f"{TRADE_DATE}T07:00:00-04:00")
    bad = dict(final_rows[0], batch_id="fixture-error", series_key="fixture-error:model")
    bad["predictions"] = predictions + predictions
    bad["variant_result_sha256"] = "fixture-error"
    failed = error_store.refresh([bad])["results"][0]
    long_case = final["results"][0]
    reserved_short_case = replay_day(
        TRADE_DATE, [{"symbol": "AAPL", "direction": "bearish"}],
        _labels(), rules, minute=final_minute,
    )
    view_cases = [
        {"name": "long", **long_case}, {"name": "empty", **empty},
        {"name": "pending", **pending}, {"name": "incomplete", **incomplete},
        {"name": "failed", **failed},
    ]

    daily_results = [{
        "batch_id": detail["batch_id"], "trade_date": TRADE_DATE,
        "variant_key": key, "series_key": row["series_key"],
        "label": variant["variant"]["label"], "model": variant["model_name"],
        "predictions": row["predictions"], "correct": len(row["predictions"]),
        "incorrect": 0, "daily_pnl": 1.0, "cumulative_pnl": 1.0,
        "status": "final", "score_eligible": True,
    } for (key, variant), row in zip(sorted(batch["results"].items()), final_rows, strict=True)]
    browser_state = {
        "product_name": "SHAQ Daily Oracle Lab", "clock": {
            "et": f"{TRADE_DATE}T16:15:00-04:00", "trade_date": TRADE_DATE,
            "is_trading_day": True, "next_trade_date": "2026-09-10",
        },
        "research_mode": {"available": True, "orders_allowed": False, "broker_modules_loaded": False},
        "operator_mode": {"platform_supported": False, "safety_ready": False, "requires_separate_setup": True},
        "formal_operator": {"connected": False},
        "settings": {
            "setup_complete": True, "model_profiles": [{
                "profile_id": profile.profile_id, "model": profile.model,
                "protocol": profile.protocol,
            }], "active_model_profile_id": profile.profile_id,
            "model_secret_saved": {}, "github": {}, "data_profile": {},
            "research_readiness": {"status": "ready"},
        },
        "versions": methods, "drafts": [], "skill_explanations": SKILL_EXPLANATIONS,
        "dashboard": {
            "daily_results": daily_results, "virtual_accounts": final,
            "batches": [{"batch_id": detail["batch_id"], "trade_date": TRADE_DATE}],
            "performance": [], "versions": [], "latest": detail,
        },
        "data_status": {"items": []},
        "jobs": [{
            "job_id": "fixture-job", "batch_id": detail["batch_id"],
            "started_at_et": f"{TRADE_DATE}T08:40:00-04:00", "status": "complete",
            "message": "两种方法夹具已完成", "variant_progress": {
                key: "complete" for key in sorted(batch["results"])
            },
        }],
    }
    comparison = registry.compare_methods(
        "independent-gate-1", "cross-domain-synthesis-1", author="team"
    )
    long_contract = _contract_trade(long_case)
    short_contract = _contract_trade(reserved_short_case)
    account_contract = {
        "rules": asdict(rules),
        "policy_id": long_case["policy_id"],
        "entry_reference_at_et": long_case["entry_reference_at_et"],
        "exit_reference_at_et": long_case["exit_reference_at_et"],
        "exit_minutes_before_close": 5,
        "long": long_contract,
        "reserved_short": short_contract,
    }
    contract_checks = {
        "exact_rules": account_contract["rules"] == {
            "initial_cash": 10000, "per_prediction_budget": 1000,
            "commission_rate": 0.0005, "slippage_rate": 0.0005,
            "schema_version": 2,
        },
        "exact_schedule": (
            account_contract["policy_id"] == "rth-minute-open-0931-close-minus5-v1"
            and account_contract["entry_reference_at_et"] == "2026-09-09T09:31:00-04:00"
            and account_contract["exit_reference_at_et"] == "2026-09-09T15:55:00-04:00"
            and account_contract["exit_minutes_before_close"] == 5
        ),
        "integer_sizing": (
            type(long_contract["quantity"]) is int
            and type(short_contract["quantity"]) is int
            and long_contract["quantity"] == short_contract["quantity"] == 9
        ),
        "long_reconciled": (
            long_contract["reconciled"]
            and long_contract["direction"] == "bullish"
            and math.isclose(long_contract["entry_reference_open"], 100.0)
            and math.isclose(long_contract["exit_reference_open"], 102.0)
            and math.isclose(long_contract["entry_price"], 100.05)
            and math.isclose(long_contract["exit_price"], 101.949)
            and math.isclose(long_contract["gross_pnl"], 18.0)
            and math.isclose(long_contract["fees"], 0.9089955)
            and math.isclose(long_contract["slippage_cost"], 0.909)
            and math.isclose(long_contract["net_pnl"], 16.1820045)
            and math.isclose(long_contract["closing_cash"], 10016.1820045)
        ),
        "reserved_short_reconciled": (
            short_contract["reconciled"]
            and short_contract["direction"] == "bearish"
            and math.isclose(short_contract["entry_reference_open"], 100.0)
            and math.isclose(short_contract["exit_reference_open"], 102.0)
            and math.isclose(short_contract["entry_price"], 99.95)
            and math.isclose(short_contract["exit_price"], 102.051)
            and math.isclose(short_contract["reserved_collateral"], 900.900225)
            and math.isclose(short_contract["gross_pnl"], -18.0)
            and math.isclose(short_contract["fees"], 0.9090045)
            and math.isclose(short_contract["slippage_cost"], 0.909)
            and math.isclose(short_contract["net_pnl"], -19.8180045)
            and math.isclose(short_contract["closing_cash"], 9980.1819955)
        ),
    }
    value = {
        "status": "passed" if all(contract_checks.values()) else "failed",
        "fixture_kind": "deterministic-model-fixture",
        "provider_secrets_used": False, "broker_modules_loaded": False,
        "methods": [{
            key: row.get(key) for key in (
                "author", "version_id", "method_name", "status_badge", "aliases"
            )
        } for row in methods],
        "variant_keys": sorted(batch["results"]),
        "evidence_hashes": {
            key: row["evidence_hash"] for key, row in sorted(batch["results"].items())
        },
        "method_comparison": comparison,
        "account_engine": final["engine"], "account_engine_version": final["engine_version"],
        "provisional_statuses": [row["status"] for row in provisional["results"]],
        "final_statuses": [row["status"] for row in final["results"]],
        "reopen_matches": reopened["accounts"] == final["accounts"] and reopened["results"] == final["results"],
        "account_contract": account_contract, "contract_checks": contract_checks,
        "view_cases": view_cases, "browser_state": browser_state,
        "batch_detail": dict(detail, research_progress=progress.read()),
    }
    _atomic_json(output_root / "browser-state.json", browser_state)
    _atomic_json(output_root / "lab-smoke-result.json", value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    value = run_lab_smoke(package_root=args.package_root, output_root=args.output)
    print(json.dumps({
        "status": value["status"], "fixture_kind": value["fixture_kind"],
        "methods": value["methods"], "account_engine": value["account_engine"],
        "final_statuses": value["final_statuses"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return 0 if value["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
