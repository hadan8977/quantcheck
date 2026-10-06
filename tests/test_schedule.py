import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from quantcheck.schedule import (
    MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES,
    NON_TRADING_DAY_SCHEDULE,
    TRADING_DAY_SCHEDULE,
    is_month_end_official_mail_day,
    month_end_official_mail_schedule,
    parse_schedule,
    schedule_for_date,
)
from quantcheck.picks_check import current_window


class ScheduleTests(unittest.TestCase):
    def test_empty_schedule_uses_dynamic_trading_day_default(self):
        self.assertEqual(parse_schedule("", current_date=date(2026, 5, 26)), TRADING_DAY_SCHEDULE)

    def test_empty_schedule_uses_non_trading_day_default(self):
        self.assertEqual(parse_schedule("", current_date=date(2026, 5, 23)), NON_TRADING_DAY_SCHEDULE)

    def test_trading_day_schedule_matches_operational_scan_plan(self):
        self.assertEqual(
            TRADING_DAY_SCHEDULE,
            [
                (8, 20, "official_mail"),
                (8, 30, "picks"),
                (8, 45, "health_site"),
                (9, 0, "picks"),
                (9, 20, "official_mail"),
                (9, 40, "picks"),
                (12, 0, "official_mail"),
                (12, 40, "daily_admin_status"),
                (17, 0, "picks"),
                (17, 15, "health_site"),
                (17, 30, "official_mail"),
            ],
        )

    def test_non_trading_day_schedule_runs_daily_picks_mail_and_admin_status(self):
        self.assertEqual(
            NON_TRADING_DAY_SCHEDULE,
            [(12, 0, "picks"), (12, 20, "official_mail"), (12, 40, "daily_admin_status")],
        )

    def test_custom_schedule_parses_kinds(self):
        self.assertEqual(
            parse_schedule("08:20:official_mail,08:30:picks,12:40:daily_admin_status,17:15:health_site"),
            [(8, 20, "official_mail"), (8, 30, "picks"), (12, 40, "daily_admin_status"), (17, 15, "health_site")],
        )

    def test_invalid_schedule_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_schedule("08:30:unknown")

    def test_schedule_for_date_treats_market_holiday_as_non_trading_day(self):
        # Memorial Day 2026: NYSE closed.
        self.assertEqual(schedule_for_date(date(2026, 5, 25)), NON_TRADING_DAY_SCHEDULE)

    def test_month_end_official_mail_days_are_last_two_calendar_days(self):
        self.assertFalse(is_month_end_official_mail_day(date(2026, 6, 28)))
        self.assertTrue(is_month_end_official_mail_day(date(2026, 6, 29)))
        self.assertTrue(is_month_end_official_mail_day(date(2026, 6, 30)))
        self.assertTrue(is_month_end_official_mail_day(date(2026, 2, 27)))
        self.assertTrue(is_month_end_official_mail_day(date(2026, 2, 28)))

    def test_month_end_official_mail_schedule_is_uniform(self):
        official = month_end_official_mail_schedule()
        self.assertEqual(official[0], (8, 0, "official_mail"))
        self.assertEqual(official[-1], (20, 0, "official_mail"))
        minute_offsets = [hour * 60 + minute for hour, minute, _ in official]
        deltas = [b - a for a, b in zip(minute_offsets, minute_offsets[1:])]
        self.assertEqual(set(deltas), {MONTH_END_OFFICIAL_MAIL_INTERVAL_MINUTES})

    def test_month_end_schedule_keeps_core_jobs_and_adds_uniform_official_mail(self):
        schedule = schedule_for_date(date(2026, 6, 30))
        self.assertIn((17, 0, "picks"), schedule)
        self.assertIn((17, 15, "health_site"), schedule)
        self.assertIn((12, 40, "daily_admin_status"), schedule)
        self.assertIn((8, 0, "official_mail"), schedule)
        self.assertIn((20, 0, "official_mail"), schedule)
        self.assertEqual(len(schedule), len(set(schedule)))

    def test_custom_schedule_does_not_get_month_end_expansion(self):
        custom = "12:00:official_mail"
        self.assertEqual(parse_schedule(custom, current_date=date(2026, 6, 30)), [(12, 0, "official_mail")])

    def test_current_window_has_no_open_0940_window_and_has_daily_non_trading(self):
        ny = ZoneInfo("America/New_York")
        self.assertEqual(current_window(datetime(2026, 5, 26, 8, 30, tzinfo=ny)), "premarket_0830")
        self.assertEqual(current_window(datetime(2026, 5, 26, 9, 0, tzinfo=ny)), "premarket_0900")
        self.assertIsNone(current_window(datetime(2026, 5, 26, 9, 40, tzinfo=ny)))
        self.assertEqual(current_window(datetime(2026, 5, 30, 12, 0, tzinfo=ny)), "daily_non_trading_1200")

    def test_default_picks_run_has_no_global_timeout(self):
        import quantcheck.scheduler as scheduler

        captured = {}

        def fake_run_cmd(args, timeout, **kwargs):
            captured["timeout"] = timeout
            return (0, "") if kwargs.get("capture_output") else 0

        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd):
            self.assertEqual(scheduler.run_picks(), 0)

        self.assertIsNone(captured["timeout"])

    def test_official_mail_forward_triggers_forced_picks_check(self):
        import quantcheck.scheduler as scheduler

        calls = []

        def fake_run_cmd(args, timeout, **kwargs):
            calls.append((args, timeout, kwargs))
            if "quantcheck.official_mail_forwarder" in args:
                return 0, '{"checked": 1, "matched": 1, "forwarded": 1, "failed": 0}'
            return 0

        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd):
            self.assertEqual(scheduler.run_official_mail(), 0)

        self.assertEqual(len(calls), 2)
        self.assertIn("quantcheck.official_mail_forwarder", calls[0][0])
        self.assertTrue(calls[0][2]["capture_output"])
        self.assertIn("quantcheck.picks_check", calls[1][0])
        self.assertIn("--force", calls[1][0])

    def test_official_mail_without_new_forward_does_not_trigger_picks_check(self):
        import quantcheck.scheduler as scheduler

        calls = []

        def fake_run_cmd(args, timeout, **kwargs):
            calls.append((args, timeout, kwargs))
            return 0, '{"checked": 1, "matched": 1, "forwarded": 0, "failed": 0}'

        with patch.dict(os.environ, {}, clear=True), patch.object(scheduler, "run_cmd", side_effect=fake_run_cmd):
            self.assertEqual(scheduler.run_official_mail(), 0)

        self.assertEqual(len(calls), 1)
        self.assertIn("quantcheck.official_mail_forwarder", calls[0][0])

    def test_month_end_official_mail_does_not_swallow_colliding_picks(self):
        # Regression: 2026-09-29/30 the 15-minute month-end official_mail grid
        # landed on 08:30/09:00/17:00 and the scheduler only ran official_mail.
        import quantcheck.scheduler as scheduler

        ny = ZoneInfo("America/New_York")
        for hour, minute in [(8, 30), (9, 0), (17, 0)]:
            now = datetime(2026, 9, 29, hour, minute, tzinfo=ny) - timedelta(minutes=1)
            _, target, kinds = scheduler.next_due_jobs(None, now=now)
            self.assertEqual((target.hour, target.minute), (hour, minute))
            self.assertEqual(kinds, ["picks", "official_mail"])

    def test_non_trading_month_end_noon_runs_picks_and_official_mail(self):
        import quantcheck.scheduler as scheduler

        ny = ZoneInfo("America/New_York")
        # 2026-05-30 is a Saturday and month-end.
        _, target, kinds = scheduler.next_due_jobs(None, now=datetime(2026, 5, 30, 11, 59, tzinfo=ny))
        self.assertEqual((target.hour, target.minute), (12, 0))
        self.assertEqual(kinds, ["picks", "official_mail"])


if __name__ == "__main__":
    unittest.main()
