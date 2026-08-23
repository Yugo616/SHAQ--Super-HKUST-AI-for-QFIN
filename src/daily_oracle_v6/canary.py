from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .hashing import sha256_payload


class CanaryError(ValueError):
    """Paper-canary safety policy rejected an intent."""


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CanaryError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanaryError(f"{field} requires an offset")
    return parsed


def build_canary_intents(
    *,
    forecasts: Iterable[dict[str, Any]],
    portfolio: dict[str, Any],
    borrowable: dict[str, bool],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if policy.get("trd_env") != "SIMULATE" or policy.get("real_trading_enabled") is not False:
        raise CanaryError("REAL trading is forbidden")
    created_at = _aware(policy["created_at"], "created_at")
    intent_created_at = _aware(policy["intent_created_at"], "intent_created_at")
    cutoff = _aware(policy["forecast_cutoff"], "forecast_cutoff")
    entry_after = _aware(policy["entry_after"], "entry_after")
    entry_deadline = _aware(policy["entry_deadline"], "entry_deadline")
    exit_at = _aware(policy["exit_at"], "exit_at")
    exit_deadline = _aware(policy["exit_deadline"], "exit_deadline")
    if not entry_after <= entry_deadline < exit_at <= exit_deadline:
        raise CanaryError("execution windows are not ordered")
    mode = (
        "canary"
        if policy.get("forecast_mode", "canary") == "canary"
        and created_at <= cutoff and intent_created_at <= cutoff
        else "shadow"
    )
    rows = sorted((dict(row) for row in forecasts), key=lambda row: row["symbol"])
    if len(rows) > int(policy["max_forecasts"]):
        raise CanaryError("forecast cap exceeded")
    symbols = [row["symbol"] for row in rows]
    if len(symbols) != len(set(symbols)):
        raise CanaryError("duplicate forecast symbol")

    account_alias = portfolio.get("account_alias")
    if portfolio.get("trd_env") != "SIMULATE":
        raise CanaryError("portfolio snapshot is not SIMULATE")
    if account_alias not in set(policy["account_allowlist"]):
        raise CanaryError("account alias is not allowlisted")
    portfolio_observed = _aware(policy["portfolio_observed_at"], "portfolio_observed_at")
    borrow_observed = _aware(policy["borrow_captured_at"], "borrow_captured_at")
    for observed, maximum, name in (
        (portfolio_observed, int(policy["max_portfolio_age_seconds"]), "portfolio"),
        (borrow_observed, int(policy["max_borrow_age_seconds"]), "borrow"),
    ):
        age = (intent_created_at - observed).total_seconds()
        if maximum <= 0 or age < 0 or age > maximum:
            raise CanaryError(f"{name} snapshot is stale or future-dated")
    external = sorted(
        position["symbol"]
        for position in portfolio.get("positions", [])
        if position.get("quantity", 0)
    )
    intents = []
    for row in rows:
        if row.get("direction") not in {"bullish", "bearish"}:
            raise CanaryError("forecast direction must be bullish or bearish")
        if row.get("score_eligible") is not True:
            raise CanaryError("ineligible forecast cannot become an intent")
        if row["symbol"] in external:
            continue
        if row["direction"] == "bearish" and not borrowable.get(row["symbol"], False):
            continue
        side = "BUY" if row["direction"] == "bullish" else "SELL_SHORT"
        seed = {
            "run_id": policy["run_id"],
            "symbol": row["symbol"],
            "side": side,
            "quantity": int(policy["shares_per_forecast"]),
            "account_alias": account_alias,
        }
        intents.append({
            **seed,
            "intent_id": "intent_" + sha256_payload(seed)[:20],
            "idempotency_key": sha256_payload({"intent": seed, "version": 6}),
            "trd_env": "SIMULATE",
            "session": "RTH",
            "order_type": "MARKET",
            "submit_after": policy["entry_after"],
            "submit_before": policy["entry_deadline"],
            "exit_at": policy["exit_at"],
            "status": "READY" if mode == "canary" else "SHADOW_ONLY",
        })
    return {
        "schema_version": 6,
        "run_id": policy["run_id"],
        "created_at": intent_created_at.isoformat(),
        "mode": mode,
        "trd_env": "SIMULATE",
        "external_positions": external,
        "intents": intents,
        "scientific_label": "official_unadjusted_US_regular_session_open_to_close",
        "trading_ledger": "actual_fill_to_actual_fill_separate_from_scientific_label",
        "p_committee_hit": None,
        "p_net_profit": None,
    }
