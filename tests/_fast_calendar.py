"""Speed up quantcheck.schedule.is_trading_day for the whole test process.

The real function asks pandas_market_calendars for a one-day schedule (~50 ms
per call, ~0.7 s for the first), and next_due_jobs()/schedule_preview() call it
once per candidate day. This replacement reads the same NYSE calendar once per
calendar year and answers from a set. `real_is_trading_day` keeps the original
so a test (test_schedule) can assert both agree on weekdays, weekends and
holidays. Import this after `_test_env`.
"""

import functools

from quantcheck import schedule

real_is_trading_day = schedule.is_trading_day


@functools.lru_cache(maxsize=None)
def _trading_days(year):
    sessions = schedule._nyse_calendar().schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    return frozenset(ts.date() for ts in sessions.index)


def fast_is_trading_day(day):
    return day in _trading_days(day.year)


if schedule.is_trading_day is real_is_trading_day:
    schedule.is_trading_day = fast_is_trading_day
