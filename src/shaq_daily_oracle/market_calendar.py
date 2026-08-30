from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


class MarketCalendarError(ValueError):
    """The authoritative US-equity session calendar is unavailable or invalid."""


@dataclass(frozen=True)
class MarketSession:
    session_date: date
    market_open: datetime
    market_close: datetime

    @property
    def early_close(self) -> bool:
        return self.market_close.hour < 16


@lru_cache(maxsize=1)
def _nyse():
    try:
        import pandas_market_calendars as mcal  # type: ignore
    except ImportError as exc:
        raise MarketCalendarError(
            "pandas-market-calendars is required for formal NYSE scheduling"
        ) from exc
    return mcal.get_calendar("NYSE")


def market_session(value: date) -> MarketSession | None:
    schedule = _nyse().schedule(start_date=value.isoformat(), end_date=value.isoformat())
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    zone = ZoneInfo("America/New_York")
    market_open = row["market_open"].to_pydatetime().astimezone(zone)
    market_close = row["market_close"].to_pydatetime().astimezone(zone)
    if market_open.date() != value or market_close.date() != value:
        raise MarketCalendarError("NYSE calendar returned a session on another local date")
    return MarketSession(value, market_open, market_close)


def next_market_session(after: date) -> MarketSession:
    start = after + timedelta(days=1)
    end = start + timedelta(days=21)
    schedule = _nyse().schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if schedule.empty:
        raise MarketCalendarError("NYSE calendar has no next session in the lookup window")
    row = schedule.iloc[0]
    zone = ZoneInfo("America/New_York")
    market_open = row["market_open"].to_pydatetime().astimezone(zone)
    market_close = row["market_close"].to_pydatetime().astimezone(zone)
    return MarketSession(market_open.date(), market_open, market_close)


def previous_market_session(before: date) -> MarketSession:
    end = before - timedelta(days=1)
    start = end - timedelta(days=21)
    schedule = _nyse().schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if schedule.empty:
        raise MarketCalendarError("NYSE calendar has no previous session in the lookup window")
    row = schedule.iloc[-1]
    zone = ZoneInfo("America/New_York")
    market_open = row["market_open"].to_pydatetime().astimezone(zone)
    market_close = row["market_close"].to_pydatetime().astimezone(zone)
    return MarketSession(market_open.date(), market_open, market_close)
