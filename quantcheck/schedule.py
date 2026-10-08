from __future__ import annotations

import calendar
from datetime import date
from functools import lru_cache

import pandas_market_calendars as mcal

TRADING_DAY_SCHEDULE = [
    # US regular open is 09:30 America/New_York. Picks: two premarket scans
    # (1h and 30min before the open) and one after the close -- exactly the
    # windows picks_check.WINDOWS accepts. All times are New York time, so
    # they follow the market through DST changes automatically.
    (8, 20, "official_mail"),
    (8, 30, "picks"),
    (8, 45, "health_site"),
    (9, 0, "picks"),
    (9, 20, "official_mail"),
    (9, 50, "weekly_digest"),
    (12, 0, "official_mail"),
    (12, 40, "daily_admin_status"),
    (17, 0, "picks"),
    (17, 15, "health_site"),
    (17, 30, "official_mail"),
]

NON_TRADING_DAY_SCHEDULE = [
    # Weekends and market holidays: one midday sweep is enough.
    (12, 0, "picks"),
    (12, 20, "official_mail"),
    (12, 30, "weekly_digest"),
    (12, 40, "daily_admin_status"),
    (18, 0, "weekly_digest"),
]

VALID_KINDS = {"picks", "health_site", "health", "official_mail", "daily_admin_status", "weekly_digest"}
MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES = 15
MONTH_END_OFFICIAL_MAIL_START = (8, 0)
MONTH_END_OFFICIAL_MAIL_END = (20, 0)


@lru_cache(maxsize=8)
def _nyse_calendar():
    return mcal.get_calendar("NYSE")


def is_trading_day(day: date) -> bool:
    cal = _nyse_calendar()
    schedule = cal.schedule(start_date=day.isoformat(), end_date=day.isoformat())
    return not schedule.empty


def schedule_for_date(day: date):
    schedule = TRADING_DAY_SCHEDULE if is_trading_day(day) else NON_TRADING_DAY_SCHEDULE
    if is_month_end_official_mail_day(day):
        return merge_schedules(schedule, month_end_official_mail_schedule())
    return schedule


def is_month_end_official_mail_day(day: date) -> bool:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return day.day >= last_day - 1


def month_end_official_mail_schedule(
    interval_minutes: int = MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES,
    start: tuple[int, int] = MONTH_END_OFFICIAL_MAIL_START,
    end: tuple[int, int] = MONTH_END_OFFICIAL_MAIL_END,
):
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    if not 0 <= start_minutes <= end_minutes <= 23 * 60 + 59:
        raise ValueError("invalid month-end official-mail window")

    out = []
    minute_of_day = start_minutes
    while minute_of_day <= end_minutes:
        out.append((minute_of_day // 60, minute_of_day % 60, "official_mail"))
        minute_of_day += interval_minutes
    return out


def merge_schedules(*schedules):
    merged = {}
    for schedule in schedules:
        for hour, minute, kind in schedule:
            key = (hour, minute, kind)
            merged[key] = (hour, minute, kind)
    return sorted(merged.values(), key=lambda item: (item[0], item[1], item[2]))


def parse_schedule(raw: str | None, current_date: date | None = None):
    if not raw:
        return schedule_for_date(current_date or date.today())
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        time_part, kind = item.rsplit(":", 1)
        hour, minute = [int(x) for x in time_part.split(":")]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"invalid schedule time: {time_part}")
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid schedule kind: {kind}")
        out.append((hour, minute, kind))
    return out or schedule_for_date(current_date or date.today())
