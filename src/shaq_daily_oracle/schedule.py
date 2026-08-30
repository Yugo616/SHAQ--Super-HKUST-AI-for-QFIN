from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .market_calendar import market_session


class ScheduleError(ValueError):
    """A workflow clock or schedule is invalid."""


def session_times(session_date: date, config: dict[str, Any]) -> dict[str, datetime]:
    zone = ZoneInfo(str(config["timezone"]))
    bindings = config.get("parameter_bindings", {})
    parameters = [
        "timezone", "precheck_start", "evidence_cutoff", "forecast_deadline",
        "entry_after", "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after", "order_poll_interval_seconds",
        "account_alias", "shares_per_forecast", "maximum_forecasts",
    ]
    if any(len(bindings.get(name, [])) != 3 for name in parameters):
        raise ScheduleError("runtime parameters require reference, decision and experiment bindings")
    session = market_session(session_date)
    if session is None:
        raise ScheduleError("session date is not an NYSE trading session")
    poll_seconds = config.get("order_poll_interval_seconds")
    if not isinstance(poll_seconds, int) or not 5 <= poll_seconds <= 30:
        raise ScheduleError("order polling interval must be an integer from 5 to 30 seconds")
    output = {}
    for name in (
        "precheck_start", "evidence_cutoff", "forecast_deadline", "entry_after",
        "entry_deadline", "exit_at", "exit_deadline",
        "label_capture_after",
    ):
        parsed = time.fromisoformat(str(config[name]))
        output[name] = datetime.combine(session_date, parsed, zone)
    output["market_open"] = session.market_open
    output["market_close"] = session.market_close
    output["early_close"] = session.early_close
    if session.early_close:
        output["exit_at"] = session.market_close - timedelta(minutes=5)
        output["exit_deadline"] = session.market_close - timedelta(minutes=2)
        output["label_capture_after"] = session.market_close + timedelta(seconds=5)
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
