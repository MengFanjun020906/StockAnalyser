# -*- coding: utf-8 -*-
"""A-share trading calendar and canonical intraday bar schedule."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_VERSION = "cn-calendar.v1"
FALLBACK_CALENDAR_VERSION = "cn-calendar.weekday-fallback.v1"

# Baostock 5-minute bars are interval-end timestamps.  The session has 48 bars.
MORNING_BAR_TIMES = [
    time(9, 35), time(9, 40), time(9, 45), time(9, 50), time(9, 55),
    time(10, 0), time(10, 5), time(10, 10), time(10, 15), time(10, 20),
    time(10, 25), time(10, 30), time(10, 35), time(10, 40), time(10, 45),
    time(10, 50), time(10, 55), time(11, 0), time(11, 5), time(11, 10),
    time(11, 15), time(11, 20), time(11, 25), time(11, 30),
]
AFTERNOON_BAR_TIMES = [
    time(13, 5), time(13, 10), time(13, 15), time(13, 20), time(13, 25),
    time(13, 30), time(13, 35), time(13, 40), time(13, 45), time(13, 50),
    time(13, 55), time(14, 0), time(14, 5), time(14, 10), time(14, 15),
    time(14, 20), time(14, 25), time(14, 30), time(14, 35), time(14, 40),
    time(14, 45), time(14, 50), time(14, 55), time(15, 0),
]
FIVE_MINUTE_BAR_TIMES = MORNING_BAR_TIMES + AFTERNOON_BAR_TIMES


class TradingCalendarError(ValueError):
    pass


class CnTradingCalendar:
    """Deterministic XSHG session calendar with an explicit weekday fallback.

    The fallback is never silent: callers can inspect ``fallback_used`` and the
    manifest records ``calendar_version``.  A strict experiment can reject a
    fallback calendar through precheck.
    """

    def __init__(self, *, allow_fallback: bool = True) -> None:
        self._calendar = None
        self._calendar_error: str | None = None
        self.allow_fallback = allow_fallback
        try:
            import exchange_calendars as xcals

            self._calendar = xcals.get_calendar("XSHG")
        except Exception as exc:  # pragma: no cover - environment dependent
            self._calendar_error = str(exc)
            logger.warning("XSHG calendar unavailable; weekday fallback enabled: %s", exc)
        self.fallback_used = self._calendar is None

    @property
    def version(self) -> str:
        return FALLBACK_CALENDAR_VERSION if self.fallback_used else CALENDAR_VERSION

    def sessions(self, start_date: date, end_date: date) -> list[date]:
        if end_date < start_date:
            return []
        if self._calendar is not None:
            try:
                index = self._calendar.sessions_in_range(start_date.isoformat(), end_date.isoformat())
                return [item.date() for item in index]
            except Exception as exc:
                if not self.allow_fallback:
                    raise TradingCalendarError(str(exc)) from exc
                logger.warning(
                    "XSHG sessions unavailable for %s..%s; weekday fallback: %s",
                    start_date,
                    end_date,
                    exc,
                )
        return [item for item in _weekdays(start_date, end_date)]

    def is_session(self, value: date) -> bool:
        return bool(self.sessions(value, value))

    def next_session(self, value: date, *, include_self: bool = False) -> date:
        start = value if include_self else value + timedelta(days=1)
        sessions = self.sessions(start, start + timedelta(days=31))
        if not sessions:
            raise TradingCalendarError(f"no session after {value.isoformat()}")
        return sessions[0]

    def previous_session(self, value: date, *, include_self: bool = False) -> date:
        end = value if include_self else value - timedelta(days=1)
        sessions = self.sessions(end - timedelta(days=31), end)
        if not sessions:
            raise TradingCalendarError(f"no session before {value.isoformat()}")
        return sessions[-1]

    def session_open(self, session: date) -> datetime:
        return datetime.combine(session, time(9, 30))

    def session_close(self, session: date) -> datetime:
        return datetime.combine(session, time(15, 0))

    def bar_times(self, session: date, granularity: str) -> list[datetime]:
        if granularity == "1d":
            return [self.session_close(session)]
        return [datetime.combine(session, item) for item in FIVE_MINUTE_BAR_TIMES]

    def count_sessions(self, start_date: date, end_date: date) -> int:
        return len(self.sessions(start_date, end_date))

    def first_session_on_or_after(self, value: date) -> date:
        return self.next_session(value, include_self=True)

    def first_session_after_datetime(self, value: datetime) -> date:
        local = as_shanghai_naive(value)
        sessions = self.sessions(local.date(), local.date() + timedelta(days=31))
        for session in sessions:
            if self.session_close(session) > local:
                return session
        return self.next_session(local.date())


def as_shanghai_naive(value: datetime) -> datetime:
    """Normalize aware datetimes to naive Asia/Shanghai wall time."""
    if value.tzinfo is None:
        return value
    return value.astimezone(SHANGHAI).replace(tzinfo=None)


def as_shanghai_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _weekdays(start_date: date, end_date: date) -> list[date]:
    result: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


@lru_cache(maxsize=4)
def get_cn_calendar(allow_fallback: bool = True) -> CnTradingCalendar:
    return CnTradingCalendar(allow_fallback=allow_fallback)


__all__ = [
    "AFTERNOON_BAR_TIMES",
    "CALENDAR_VERSION",
    "CnTradingCalendar",
    "FALLBACK_CALENDAR_VERSION",
    "FIVE_MINUTE_BAR_TIMES",
    "MORNING_BAR_TIMES",
    "SHANGHAI",
    "TradingCalendarError",
    "as_shanghai_aware",
    "as_shanghai_naive",
    "get_cn_calendar",
]
