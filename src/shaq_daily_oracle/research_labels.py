from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .data_providers import DataProfile, OpenBBProviderAdapter, YFinanceProvider
from .hashing import sha256_payload
from .research_batch import load_frozen_evidence
from .market_calendar import market_session, next_market_session


class ResearchLabelError(ValueError):
    """A research label does not match the governed unadjusted RTH definition."""


ET = ZoneInfo("America/New_York")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bar_for_date(records: list[dict[str, Any]], trade_date: date) -> dict[str, Any] | None:
    matches = []
    for row in records:
        timestamp = str(row.get("timestamp", ""))
        try:
            observed_date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                observed_date = date.fromisoformat(timestamp[:10])
            except ValueError:
                continue
        opening, closing = _number(row.get("open")), _number(row.get("close"))
        if observed_date == trade_date and opening not in {None, 0} and closing is not None:
            matches.append((opening, closing))
    if len(matches) != 1:
        return None
    opening, closing = matches[0]
    return {
        "official_unadjusted_open": opening,
        "official_unadjusted_close": closing,
        "open_to_close_return": closing / opening - 1,
        "actual_direction": (
            "bullish" if closing > opening else ("bearish" if closing < opening else "neutral")
        ),
    }


def _observation_time(row: dict[str, Any]) -> datetime:
    value = datetime.fromisoformat(str(row["observed_at_et"]).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ResearchLabelError("label observation timestamp must include a timezone")
    return value.astimezone(ET)


def _price_identity(row: dict[str, Any]) -> tuple[float | None, float | None]:
    return (_number(row.get("official_unadjusted_open")),
            _number(row.get("official_unadjusted_close")))


def recompute_label(existing: dict[str, Any]) -> dict[str, Any]:
    """Derive status from immutable observations, including legacy unsorted rows."""
    observations = sorted(
        (dict(row) for row in existing.get("observations", [])),
        key=lambda row: (_observation_time(row), str(row.get("observation_sha256", ""))),
    )
    result = dict(existing)
    result["observations"] = observations
    if not observations:
        result.update(status="provisional", confirmed_by_independent_reobservation=False)
        return result

    corrections = list(existing.get("corrections", []))
    known = {(str(row.get("previous_observation_sha256", "")),
              str(row.get("revised_observation_sha256", ""))) for row in corrections}
    previous = observations[0]
    for current in observations[1:]:
        pair = (str(previous.get("observation_sha256", "")),
                str(current.get("observation_sha256", "")))
        if _price_identity(previous) != _price_identity(current) and pair not in known:
            corrections.append({
                "detected_at_et": current["observed_at_et"],
                "previous_observation_sha256": pair[0],
                "revised_observation_sha256": pair[1],
                "previous_open": _price_identity(previous)[0],
                "previous_close": _price_identity(previous)[1],
                "revised_open": _price_identity(current)[0],
                "revised_close": _price_identity(current)[1],
            })
            known.add(pair)
        previous = current

    current_identity = _price_identity(observations[-1])
    segment = []
    for row in reversed(observations):
        if _price_identity(row) != current_identity:
            break
        segment.append(row)
    segment.reverse()
    first_time = _observation_time(segment[0])
    eligible_day = next_market_session(first_time.date()).session_date
    confirmed = any(_observation_time(row).date() >= eligible_day for row in segment[1:])
    latest = observations[-1]
    result.update({key: latest[key] for key in (
        "official_unadjusted_open", "official_unadjusted_close",
        "open_to_close_return", "actual_direction",
    ) if key in latest})
    result.update({
        "corrections": corrections,
        "status": "final" if confirmed else "provisional",
        "confirmed_by_independent_reobservation": confirmed,
        "last_checked_at_et": latest["observed_at_et"],
        "earliest_eligible_confirmation_trading_day": eligible_day.isoformat(),
        "confirmation_reason": (
            "matching_price_observed_on_later_trading_day" if confirmed
            else "waiting_for_matching_observation_on_later_trading_day"
        ),
    })
    return result


def refresh_research_labels(
    *,
    research_root: Path,
    batches_root: Path,
    profile: DataProfile,
    observed_at: datetime | None = None,
    market_provider: Any | None = None,
    openbb_api_key: str = "",
) -> dict[str, Any]:
    now = (observed_at or datetime.now(ET)).astimezone(ET)
    if market_provider is None:
        market_provider = (
            OpenBBProviderAdapter(
                profile=profile,
                api_key=openbb_api_key,
            )
            if profile.market_provider == "openbb-rest" else YFinanceProvider(profile)
        )
    refreshed = []
    failures = []
    for batch_root in sorted(batches_root.glob("LAB-*")):
        try:
            manifest_path = batch_root / "batch_manifest.json"
            if not manifest_path.is_file():
                continue
            batch_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            trade_date = date.fromisoformat(str(batch_root.name[4:14]))
            session = market_session(trade_date)
            if session is None or now <= session.market_close:
                continue
            evidence_hash = str(batch_manifest.get("batch_identity", {}).get("evidence_hash", ""))
            path = batch_root / "labels.json"
            document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
                "schema_version": 1,
                "prediction_target": "official_unadjusted_US_regular_session_open_to_close",
                "session_scope": "US_regular_session",
                "price_adjustment": "unadjusted",
                "flat_outcome_policy": "neutral_counts_in_denominator_and_is_wrong_for_directional_forecasts",
                "labels": {},
            }
            repaired = False
            for symbol, saved in list(document.get("labels", {}).items()):
                if saved.get("observations"):
                    computed = recompute_label(saved)
                    repaired = repaired or computed != saved
                    document["labels"][symbol] = computed
            if repaired:
                unsigned = {key: value for key, value in document.items() if key != "labels_sha256"}
                document["labels_sha256"] = sha256_payload(unsigned)
                _atomic_json(path, document)
            evidence = load_frozen_evidence(research_root / "evidence" / evidence_hash)
            symbols = [row["symbol"] for row in evidence.candidate_intake["candidates"]]
            rows = market_provider.history(
                symbols, start=trade_date, end=trade_date + timedelta(days=1), interval="1d"
            )
            missing_symbols = []
            for symbol in symbols:
                label = _bar_for_date(rows.get(symbol, []), trade_date)
                if label is None:
                    missing_symbols.append(symbol)
                    continue
                observation = {
                    "observed_at_et": now.isoformat(),
                    "provider": profile.market_provider,
                    **label,
                }
                observation["observation_sha256"] = sha256_payload(observation)
                existing = document["labels"].setdefault(symbol, {"observations": []})
                observations = existing["observations"]
                if not observations or observations[-1]["observation_sha256"] != observation[
                    "observation_sha256"
                ]:
                    observations.append(observation)
                existing.update(recompute_label(existing))
            unsigned = {key: value for key, value in document.items() if key != "labels_sha256"}
            document["labels_sha256"] = sha256_payload(unsigned)
            _atomic_json(path, document)
            if missing_symbols:
                failures.append({
                    "batch_id": batch_root.name,
                    "error_type": "MissingDailyPrice",
                    "message": "missing expected daily price: " + ", ".join(missing_symbols),
                    "missing_symbols": missing_symbols,
                })
            else:
                refreshed.append(batch_root.name)
        except Exception as exc:
            failures.append({
                "batch_id": batch_root.name,
                "error_type": type(exc).__name__, "message": str(exc),
            })
    return {"refreshed_batches": refreshed, "failures": failures}
