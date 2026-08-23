from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo


class ScheduleError(ValueError):
    """A workflow clock or schedule is invalid."""


def session_times(session_date: date, config: dict[str, Any]) -> dict[str, datetime]:
    zone = ZoneInfo(str(config["timezone"]))
    bindings = config.get("parameter_bindings", {})
    parameters = [
        "timezone", "precheck_start", "evidence_cutoff", "forecast_deadline",
        "entry_after", "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after",
        "account_alias", "shares_per_forecast", "maximum_forecasts",
    ]
    if any(len(bindings.get(name, [])) != 3 for name in parameters):
        raise ScheduleError("runtime parameters require reference, decision and experiment bindings")
    if session_date.weekday() >= 5:
        raise ScheduleError("session date must be a weekday")
    output = {}
    for name in (
        "precheck_start", "evidence_cutoff", "forecast_deadline", "entry_after",
        "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after",
    ):
        parsed = time.fromisoformat(str(config[name]))
        output[name] = datetime.combine(session_date, parsed, zone)
    ordered = [output[name] for name in (
        "precheck_start", "evidence_cutoff", "forecast_deadline", "entry_after",
        "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after",
    )]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise ScheduleError("runtime stages are not strictly ordered")
    return output


def formal_mode(now: datetime, schedule: dict[str, datetime]) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleError("workflow time requires an offset")
    return "paper" if now <= schedule["evidence_cutoff"] else "shadow"
