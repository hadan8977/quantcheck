import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import _fast_calendar  # -- fast trading-day lookup for the whole suite
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import quantcheck.scheduler as scheduler
from quantcheck.picks_check import WINDOWS, current_window
from quantcheck.schedule import (
    MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES,
    NON_TRADING_DAY_SCHEDULE,
    TRADING_DAY_SCHEDULE,
    is_month_end_official_mail_day,
    month_end_official_mail_schedule,
    parse_schedule,
    schedule_for_date,
)

NY = ZoneInfo("America/New_York")


class ScheduleTests(unittest.TestCase):
    def test_schedules_match_the_operational_scan_plan(self):
        self.assertEqual(
            TRADING_DAY_SCHEDULE,
            [
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
            ],
        )
        self.assertEqual(
            NON_TRADING_DAY_SCHEDULE,
            [(12, 0, "picks"), (12, 20, "official_mail"), (12, 30, "weekly_digest"), (12, 40, "daily_admin_status"), (18, 0, "weekly_digest")],
        )

    def test_default_schedule_follows_trading_days_and_market_holidays(self):
        cases = {
            "weekday": (date(2026, 5, 26), TRADING_DAY_SCHEDULE),
            "saturday": (date(2026, 5, 23), NON_TRADING_DAY_SCHEDULE),
            "Memorial Day 2026 (NYSE closed)": (date(2026, 5, 25), NON_TRADING_DAY_SCHEDULE),
        }
        for name, (day, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(parse_schedule("", current_date=day), expected)
                self.assertEqual(schedule_for_date(day), expected)

    def test_fast_test_calendar_agrees_with_the_real_trading_day_lookup(self):
        # The suite swaps in _fast_calendar.fast_is_trading_day for speed; prove it matches the real one.
        for day in (date(2026, 5, 26), date(2026, 5, 23), date(2026, 5, 25), date(2026, 4, 3), date(2026, 7, 3), date(2026, 11, 26), date(2026, 12, 25)):
            with self.subTest(day=day):
                self.assertIs(_fast_calendar.fast_is_trading_day(day), _fast_calendar.real_is_trading_day(day))

    def test_custom_schedule_parses_kinds_rejects_bad_ones_and_skips_month_end_expansion(self):
        self.assertEqual(
            parse_schedule("08:20:official_mail,08:30:picks,12:40:daily_admin_status,17:15:health_site"),
            [(8, 20, "official_mail"), (8, 30, "picks"), (12, 40, "daily_admin_status"), (17, 15, "health_site")],
        )
        self.assertEqual(parse_schedule("12:00:official_mail", current_date=date(2026, 6, 30)), [(12, 0, "official_mail")])
        for bad in ("08:30:unknown", "25:00:picks", "08:61:picks"):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_schedule(bad)

    def test_month_end_official_mail_days_are_last_two_calendar_days(self):
        for day, expected in {
            date(2026, 6, 28): False,
            date(2026, 6, 29): True,
            date(2026, 6, 30): True,
            date(2026, 2, 27): True,
            date(2026, 2, 28): True,
        }.items():
            with self.subTest(day=day):
                self.assertIs(is_month_end_official_mail_day(day), expected)

    def test_month_end_schedule_is_uniform_and_keeps_core_jobs(self):
        official = month_end_official_mail_schedule()
        self.assertEqual((official[0], official[-1]), ((8, 0, "official_mail"), (20, 0, "official_mail")))
        minute_offsets = [hour * 60 + minute for hour, minute, _ in official]
        self.assertEqual({b - a for a, b in zip(minute_offsets, minute_offsets[1:])}, {MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES})
        for bad in ({"interval_minutes": 0}, {"start": (20, 0), "end": (8, 0)}):
            with self.subTest(bad), self.assertRaises(ValueError):
                month_end_official_mail_schedule(**bad)

        schedule = schedule_for_date(date(2026, 6, 30))
        for entry in [(17, 0, "picks"), (17, 15, "health_site"), (12, 40, "daily_admin_status"), (8, 0, "official_mail"), (20, 0, "official_mail")]:
            self.assertIn(entry, schedule)
        self.assertEqual(len(schedule), len(set(schedule)))

    def test_every_scheduled_picks_time_is_an_accepted_scan_window(self):
        windows = set(WINDOWS.values())
        for schedule in (TRADING_DAY_SCHEDULE, NON_TRADING_DAY_SCHEDULE):
            for hour, minute, kind in schedule:
                if kind == "picks":
                    self.assertIn((hour, minute), windows, f"picks at {hour:02d}:{minute:02d} would always skip itself")

    def test_current_window_has_no_open_0940_window_and_has_daily_non_trading(self):
        self.assertEqual(current_window(datetime(2026, 5, 26, 8, 30, tzinfo=NY)), "premarket_0830")
        self.assertEqual(current_window(datetime(2026, 5, 26, 9, 0, tzinfo=NY)), "premarket_0900")
        self.assertIsNone(current_window(datetime(2026, 5, 26, 9, 40, tzinfo=NY)))
        self.assertEqual(current_window(datetime(2026, 5, 30, 12, 0, tzinfo=NY)), "daily_non_trading_1200")


class SchedulerTests(unittest.TestCase):
    def test_default_picks_run_has_no_global_timeout(self):
        captured = {}

        def fake_run_cmd(args, timeout, **kwargs):
            captured["timeout"] = timeout
            return (0, "") if kwargs.get("capture_output") else 0

        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd):
            self.assertEqual(scheduler.run_picks(), 0)

        self.assertIsNone(captured["timeout"])

    def test_official_mail_triggers_forced_picks_check_only_when_something_was_forwarded(self):
        calls = []

        def fake_run_cmd(forwarded):
            def run(args, timeout, **kwargs):
                calls.append((args, timeout, kwargs))
                if "quantcheck.official_mail_forwarder" in args:
                    return 0, '{"checked": 1, "matched": 1, "forwarded": %d, "failed": 0}' % forwarded
                return 0

            return run

        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd(0)):
            self.assertEqual(scheduler.run_official_mail(), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("quantcheck.official_mail_forwarder", calls[0][0])

        calls.clear()
        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd(1)):
            self.assertEqual(scheduler.run_official_mail(), 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][2]["capture_output"])
        self.assertIn("quantcheck.picks_check", calls[1][0])
        self.assertIn("--force", calls[1][0])

    def test_colliding_jobs_all_run_with_picks_first(self):
        # Regression: 2026-09-29/30 the 15-minute month-end official_mail grid landed on
        # 08:30/09:00/17:00 picks and the scheduler only ran official_mail.
        for hour, minute in [(8, 30), (9, 0), (17, 0)]:
            with self.subTest(slot=(hour, minute)):
                now = datetime(2026, 9, 29, hour, minute, tzinfo=NY) - timedelta(minutes=1)
                _, target, kinds = scheduler.next_due_jobs(None, now=now)
                self.assertEqual((target.hour, target.minute), (hour, minute))
                self.assertEqual(kinds, ["picks", "official_mail"])

        # 2026-05-30 is a Saturday and month-end.
        _, target, kinds = scheduler.next_due_jobs(None, now=datetime(2026, 5, 30, 11, 59, tzinfo=NY))
        self.assertEqual((target.hour, target.minute), (12, 0))
        self.assertEqual(kinds, ["picks", "official_mail"])

    def test_sleep_across_dst_is_real_elapsed_time(self):
        # (now, local time of next job, expected seconds)
        cases = {
            "fall back": (datetime(2026, 10, 31, 20, 0, tzinfo=NY), 17 * 3600),  # Sat EDT -> Sun 12:00 EST: 00:00Z -> 17:00Z, not 16h of wall clock
            "spring forward": (datetime(2027, 3, 13, 20, 0, tzinfo=NY), 15 * 3600),  # Sat EST -> Sun 12:00 EDT: 01:00Z -> 16:00Z
        }
        for name, (now, expected_seconds) in cases.items():
            with self.subTest(name):
                seconds, target, kinds = scheduler.next_due_jobs(None, now)
                self.assertEqual((target.hour, target.minute, kinds[0]), (12, 0, "picks"))
                self.assertEqual(seconds, expected_seconds)


if __name__ == "__main__":
    unittest.main()
