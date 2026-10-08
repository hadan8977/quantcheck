import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quantcheck import membership

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def ny(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NY)


class AnchorTests(unittest.TestCase):
    def test_anchor_is_ninth_at_midnight_ny_for_every_month(self):
        # Day 9 exists in every month (unlike day 31), so no clamping is needed.
        self.assertEqual(membership.anchor_at(2026, 8), ny(2026, 8, 9))
        self.assertEqual(membership.anchor_at(2026, 8).tzinfo, NY)
        for month in range(1, 13):
            with self.subTest(month=month):
                self.assertEqual(membership.anchor_at(2026, month).day, membership.ANCHOR_DAY)

    def test_next_anchor(self):
        # An instant exactly on an anchor belongs to the window *starting* there.
        cases = {
            "exactly on anchor rolls to next month": (ny(2026, 9, 9), ny(2026, 10, 9)),
            "one second before anchor": (ny(2026, 9, 8, 23, 59, 59), ny(2026, 9, 9)),
            "one second after anchor": (ny(2026, 9, 9, 0, 0, 1), ny(2026, 10, 9)),
            "mid month": (ny(2026, 8, 20, 12), ny(2026, 9, 9)),
            "year boundary": (ny(2026, 12, 15), ny(2027, 1, 9)),
            # 2026-08-10 00:30 NY == 04:30 UTC (EDT).
            "non-NY tz-aware input": (datetime(2026, 8, 10, 4, 30, tzinfo=UTC), ny(2026, 9, 9)),
        }
        for name, (given, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(membership.next_anchor(given), expected)

    def test_naive_datetimes_are_rejected(self):
        naive = datetime(2026, 8, 10)
        for name, call in {
            "next_anchor": lambda: membership.next_anchor(naive),
            "expiry_for_new_member": lambda: membership.expiry_for_new_member(naive, 1),
            "is_active": lambda: membership.is_active(ny(2026, 9, 9), naive),
        }.items():
            with self.subTest(name), self.assertRaises(ValueError):
                call()

    def test_window_of(self):
        cases = {
            "instant on anchor belongs to the new window": (ny(2026, 9, 9), ny(2026, 9, 9), ny(2026, 10, 9)),
            "instant before anchor belongs to previous window": (ny(2026, 9, 8, 23, 59, 59), ny(2026, 8, 9), ny(2026, 9, 9)),
            "year boundary": (ny(2026, 12, 20), ny(2026, 12, 9), ny(2027, 1, 9)),
        }
        for name, (given, start, end) in cases.items():
            with self.subTest(name):
                self.assertEqual(membership.window_of(given), (start, end))


class ExpiryForNewMemberTests(unittest.TestCase):
    def test_expiry_for_new_member(self):
        cases = {
            "1 month from mid-month join": (ny(2026, 8, 10), 1, ny(2026, 9, 9)),
            "3 months from mid-month join": (ny(2026, 8, 10), 3, ny(2026, 11, 9)),
            # No proration: a one-hour membership is the literal, intended rule.
            "join an hour before anchor -> one-hour membership": (ny(2026, 9, 8, 23), 1, ny(2026, 9, 9)),
            "join exactly on anchor counts a full month": (ny(2026, 9, 9), 1, ny(2026, 10, 9)),
            "12 months crosses the year": (ny(2026, 8, 10), 12, ny(2027, 8, 9)),
            "december join crosses the year": (ny(2026, 12, 9), 1, ny(2027, 1, 9)),
        }
        for name, (joined, months, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(membership.expiry_for_new_member(joined, months), expected)

    def test_rejects_non_positive_or_non_int_months(self):
        for months in (0, -1, True, 1.5):
            with self.subTest(months=months):
                with self.assertRaises(ValueError):
                    membership.expiry_for_new_member(ny(2026, 8, 10), months)
                with self.assertRaises(ValueError):
                    membership.extend_expiry(ny(2026, 9, 9), months, now=ny(2026, 8, 1))


class ExtendExpiryTests(unittest.TestCase):
    def test_extend_expiry(self):
        now = ny(2026, 8, 15, 10)
        restart = membership.expiry_for_new_member(now, 1)
        cases = {
            # Result anchors to current_expiry, not to today.
            "active expiry stacks": (ny(2026, 9, 9), 3, ny(2026, 8, 15), ny(2026, 12, 9)),
            "expired restarts from now": (ny(2026, 7, 9), 1, now, restart),
            "no record restarts from now": (None, 1, now, restart),
            # is_active is strict (now < expires_at), so equal-to-now is expired.
            "expiry == now counts as expired": (ny(2026, 9, 9), 1, ny(2026, 9, 9), ny(2026, 10, 9)),
            # set_expiry allows arbitrary dates: add calendar months, clamp short months.
            "non-anchor expiry keeps its day, clamps Feb": (ny(2026, 1, 31), 1, ny(2026, 1, 1), ny(2026, 2, 28)),
        }
        self.assertEqual(restart, ny(2026, 9, 9))
        for name, (current, months, at, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(membership.extend_expiry(current, months, now=at), expected)


class IsActiveTests(unittest.TestCase):
    def test_is_active(self):
        expiry = ny(2026, 9, 9)
        cases = {
            "no expiry is always active": (None, ny(2099, 1, 1), True),
            "before expiry": (expiry, ny(2026, 9, 8, 23, 59, 59), True),
            "at expiry": (expiry, ny(2026, 9, 9), False),
            "after expiry": (expiry, ny(2026, 9, 9, 0, 0, 1), False),
            # 2026-09-09 00:00 NY == 04:00 UTC (EDT): equality is detected across timezones.
            "cross-tz instant at expiry": (expiry, datetime(2026, 9, 9, 4, 0, 0, tzinfo=UTC), False),
            "cross-tz one second before": (expiry, datetime(2026, 9, 9, 3, 59, 59, tzinfo=UTC), True),
            # 2am-3am on 2026-03-08 does not exist in NY; pick a safe instant before the jump.
            "just before spring forward": (ny(2026, 3, 9), ny(2026, 3, 8, 1, 30), True),
        }
        for name, (expires_at, now, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(membership.is_active(expires_at, now), expected)


class DstTests(unittest.TestCase):
    """Anchors are always at 00:00:00 local time, and US DST transitions in
    America/New_York always occur between 01:00 and 03:00 local time, so an
    anchor can never land inside the ambiguous ("fall back") or nonexistent
    ("spring forward") wall-clock hour. These tests pin that property down
    across several years, including 2025 where the March transition Sunday is
    the 9th itself.
    """

    def test_march_and_november_anchors_are_unambiguous_across_years(self):
        for year in (2024, 2025, 2026, 2027, 2028):
            for month in (3, 11):
                with self.subTest(year=year, month=month):
                    dt = membership.anchor_at(year, month)
                    # Same UTC offset for fold=0 and fold=1 => not in a repeated hour.
                    self.assertEqual(dt.replace(fold=0).utcoffset(), dt.replace(fold=1).utcoffset())

    def test_2025_march_transition_sunday_is_the_anchor_day_itself(self):
        # The sharp edge case: spring-forward Sunday is the 9th. Midnight is
        # still before the 2am jump, so it is unambiguously EST (UTC-5).
        self.assertEqual(datetime(2025, 3, 9).weekday(), 6)  # Sunday
        self.assertEqual(membership.anchor_at(2025, 3).utcoffset(), timedelta(hours=-5))

    def test_windows_spanning_dst_transitions_stay_chronologically_ordered(self):
        # 2026 spring-forward is March 8 (Feb->Mar window), fall-back is November 1 (Oct->Nov window).
        for given, start_expected, end_expected in (
            (ny(2026, 2, 20), ny(2026, 2, 9), ny(2026, 3, 9)),
            (ny(2026, 10, 20), ny(2026, 10, 9), ny(2026, 11, 9)),
        ):
            with self.subTest(given=given):
                start, end = membership.window_of(given)
                self.assertEqual((start, end), (start_expected, end_expected))
                self.assertLess(start, end)
                self.assertLess(start.astimezone(UTC), end.astimezone(UTC))


if __name__ == "__main__":
    unittest.main()
