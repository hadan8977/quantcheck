import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quantcheck import membership

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def ny(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NY)


class AnchorAtTests(unittest.TestCase):
    def test_anchor_is_ninth_at_midnight_ny(self):
        dt = membership.anchor_at(2026, 8)
        self.assertEqual(dt, ny(2026, 8, 9, 0, 0, 0))
        self.assertEqual(dt.tzinfo, NY)

    def test_anchor_exists_for_every_month_including_february(self):
        # Day 9 exists in every month (unlike day 31), so no clamping is needed.
        for month in range(1, 13):
            dt = membership.anchor_at(2026, month)
            self.assertEqual(dt.day, membership.ANCHOR_DAY)


class NextAnchorTests(unittest.TestCase):
    def test_exactly_on_anchor_rolls_to_next_month(self):
        # 9/9 00:00:00 lands exactly on an anchor; that instant belongs to the
        # window *starting* there, so the next anchor is one month later.
        self.assertEqual(membership.next_anchor(ny(2026, 9, 9, 0, 0, 0)), ny(2026, 10, 9, 0, 0, 0))

    def test_one_second_before_anchor(self):
        self.assertEqual(membership.next_anchor(ny(2026, 9, 8, 23, 59, 59)), ny(2026, 9, 9, 0, 0, 0))

    def test_one_second_after_anchor_still_rolls_to_next_month(self):
        self.assertEqual(membership.next_anchor(ny(2026, 9, 9, 0, 0, 1)), ny(2026, 10, 9, 0, 0, 0))

    def test_mid_month(self):
        self.assertEqual(membership.next_anchor(ny(2026, 8, 20, 12, 0, 0)), ny(2026, 9, 9, 0, 0, 0))

    def test_crosses_year_boundary(self):
        self.assertEqual(membership.next_anchor(ny(2026, 12, 15, 0, 0, 0)), ny(2027, 1, 9, 0, 0, 0))

    def test_accepts_non_ny_tzaware_input(self):
        # 2026-08-10 00:30 NY == 2026-08-10 04:30 UTC (NY is UTC-4 in August, EDT).
        utc_dt = datetime(2026, 8, 10, 4, 30, 0, tzinfo=UTC)
        self.assertEqual(membership.next_anchor(utc_dt), ny(2026, 9, 9, 0, 0, 0))

    def test_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            membership.next_anchor(datetime(2026, 8, 10))


class WindowOfTests(unittest.TestCase):
    def test_instant_on_anchor_belongs_to_new_window(self):
        start, end = membership.window_of(ny(2026, 9, 9, 0, 0, 0))
        self.assertEqual((start, end), (ny(2026, 9, 9, 0, 0, 0), ny(2026, 10, 9, 0, 0, 0)))

    def test_instant_just_before_anchor_belongs_to_previous_window(self):
        start, end = membership.window_of(ny(2026, 9, 8, 23, 59, 59))
        self.assertEqual((start, end), (ny(2026, 8, 9, 0, 0, 0), ny(2026, 9, 9, 0, 0, 0)))

    def test_window_crosses_year_boundary(self):
        start, end = membership.window_of(ny(2026, 12, 20, 0, 0, 0))
        self.assertEqual((start, end), (ny(2026, 12, 9, 0, 0, 0), ny(2027, 1, 9, 0, 0, 0)))


class ExpiryForNewMemberTests(unittest.TestCase):
    def test_one_month_from_mid_month_join(self):
        # 8/10 join, buy 1 month -> 9/9 00:00.
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 8, 10), 1), ny(2026, 9, 9, 0, 0, 0))

    def test_three_months_from_mid_month_join(self):
        # 8/10 join, buy 3 months -> 11/9 00:00.
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 8, 10), 3), ny(2026, 11, 9, 0, 0, 0))

    def test_literal_rule_gives_a_one_hour_membership(self):
        # 9/8 23:00 join, buy 1 month -> 9/9 00:00. Only one hour of paid
        # membership; this is the literal, intended behavior of anchor-synced
        # billing (no proration), not a bug.
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 9, 8, 23, 0, 0), 1), ny(2026, 9, 9, 0, 0, 0))

    def test_join_exactly_on_anchor_still_counts_full_month_from_next_anchor(self):
        # Joining exactly at 9/9 00:00 means next_anchor is 10/9 (the anchor
        # itself starts a new window), so 1 month expires 10/9.
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 9, 9, 0, 0, 0), 1), ny(2026, 10, 9, 0, 0, 0))

    def test_twelve_month_cycle_crosses_year_boundary(self):
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 8, 10), 12), ny(2027, 8, 9, 0, 0, 0))

    def test_crosses_year_boundary_from_december(self):
        # 12/9 + 1 month = next year 1/9.
        self.assertEqual(membership.expiry_for_new_member(ny(2026, 12, 9, 0, 0, 0), 1), ny(2027, 1, 9, 0, 0, 0))

    def test_rejects_zero_or_negative_months(self):
        with self.assertRaises(ValueError):
            membership.expiry_for_new_member(ny(2026, 8, 10), 0)
        with self.assertRaises(ValueError):
            membership.expiry_for_new_member(ny(2026, 8, 10), -1)


class ExtendExpiryTests(unittest.TestCase):
    def test_stacks_on_top_of_active_expiry_not_from_today(self):
        # Currently expires 9/9; renew 3 months -> 12/9. "now" is deliberately
        # far from both dates to prove the result is anchored to
        # current_expiry, not to today.
        result = membership.extend_expiry(ny(2026, 9, 9, 0, 0, 0), 3, now=ny(2026, 8, 15, 0, 0, 0))
        self.assertEqual(result, ny(2026, 12, 9, 0, 0, 0))

    def test_expired_membership_restarts_from_now(self):
        current_expiry = ny(2026, 7, 9, 0, 0, 0)  # already in the past
        now = ny(2026, 8, 15, 10, 0, 0)
        result = membership.extend_expiry(current_expiry, 1, now=now)
        self.assertEqual(result, membership.expiry_for_new_member(now, 1))
        self.assertEqual(result, ny(2026, 9, 9, 0, 0, 0))

    def test_no_record_restarts_from_now(self):
        now = ny(2026, 8, 15, 10, 0, 0)
        result = membership.extend_expiry(None, 1, now=now)
        self.assertEqual(result, membership.expiry_for_new_member(now, 1))

    def test_expiry_exactly_equal_to_now_counts_as_expired(self):
        # is_active semantics are strict (now < expires_at), so an expiry
        # equal to now is not active and extend_expiry restarts from now.
        now = ny(2026, 9, 9, 0, 0, 0)
        result = membership.extend_expiry(now, 1, now=now)
        self.assertEqual(result, membership.expiry_for_new_member(now, 1))

    def test_extend_on_non_anchor_aligned_expiry_preserves_day(self):
        # set_expiry allows arbitrary manual dates; extending should add
        # calendar months and clamp only if the target month is short.
        current_expiry = ny(2026, 1, 31, 0, 0, 0)
        now = ny(2026, 1, 1, 0, 0, 0)
        result = membership.extend_expiry(current_expiry, 1, now=now)
        # 2026 is not a leap year, so Feb has 28 days.
        self.assertEqual(result, ny(2026, 2, 28, 0, 0, 0))

    def test_rejects_zero_or_negative_months(self):
        with self.assertRaises(ValueError):
            membership.extend_expiry(ny(2026, 9, 9), 0, now=ny(2026, 8, 1))


class IsActiveTests(unittest.TestCase):
    def test_none_expiry_is_always_active(self):
        self.assertTrue(membership.is_active(None, ny(2099, 1, 1)))

    def test_before_expiry_is_active(self):
        self.assertTrue(membership.is_active(ny(2026, 9, 9, 0, 0, 0), ny(2026, 9, 8, 23, 59, 59)))

    def test_at_expiry_is_not_active(self):
        self.assertFalse(membership.is_active(ny(2026, 9, 9, 0, 0, 0), ny(2026, 9, 9, 0, 0, 0)))

    def test_after_expiry_is_not_active(self):
        self.assertFalse(membership.is_active(ny(2026, 9, 9, 0, 0, 0), ny(2026, 9, 9, 0, 0, 1)))

    def test_cross_timezone_instant_equality_is_detected(self):
        # 2026-09-09 00:00 NY == 2026-09-09 04:00 UTC (NY is UTC-4, EDT, in September).
        expires_at = ny(2026, 9, 9, 0, 0, 0)
        now_utc = datetime(2026, 9, 9, 4, 0, 0, tzinfo=UTC)
        self.assertFalse(membership.is_active(expires_at, now_utc))
        now_utc_before = datetime(2026, 9, 9, 3, 59, 59, tzinfo=UTC)
        self.assertTrue(membership.is_active(expires_at, now_utc_before))


class DstTests(unittest.TestCase):
    """Anchors are always at 00:00:00 local time, and US DST transitions in
    America/New_York always occur between 01:00 and 03:00 local time,
    so an anchor can never land inside the ambiguous ("fall back") or
    nonexistent ("spring forward") wall-clock hour. These tests pin that
    property down across several years, including a year where the March
    transition Sunday is the 9th itself (2025).
    """

    def test_march_and_november_anchors_are_unambiguous_across_years(self):
        for year in (2024, 2025, 2026, 2027, 2028):
            for month in (3, 11):
                dt = membership.anchor_at(year, month)
                # If fold=0 and fold=1 give the same UTC offset, the local
                # time is unambiguous (not inside a repeated DST hour).
                self.assertEqual(
                    dt.replace(fold=0).utcoffset(),
                    dt.replace(fold=1).utcoffset(),
                    f"anchor for {year}-{month} is ambiguous",
                )

    def test_2025_march_transition_sunday_is_the_anchor_day_itself(self):
        # Confirms the fixture actually exercises the sharp edge case: the
        # 2nd Sunday of March 2026 (US spring-forward day) is the 9th... no,
        # 2025's is. This test locks in that fact so the property above is
        # known to cover a real transition-on-anchor-day year.
        self.assertEqual(datetime(2025, 3, 9).weekday(), 6)  # Sunday
        dt = membership.anchor_at(2025, 3)
        # Midnight on transition day is still before the 2am jump, so it is
        # unambiguously EST (UTC-5).
        self.assertEqual(dt.utcoffset(), timedelta(hours=-5))

    def test_window_spanning_spring_forward_is_still_chronologically_ordered(self):
        # 2026 spring-forward is March 8. The Feb->Mar window contains it.
        start, end = membership.window_of(ny(2026, 2, 20))
        self.assertEqual(start, ny(2026, 2, 9, 0, 0, 0))
        self.assertEqual(end, ny(2026, 3, 9, 0, 0, 0))
        self.assertLess(start, end)
        # Absolute (UTC) ordering agrees with local ordering across the jump.
        self.assertLess(start.astimezone(UTC), end.astimezone(UTC))

    def test_window_spanning_fall_back_is_still_chronologically_ordered(self):
        # 2026 fall-back is November 1. The Oct->Nov window contains it.
        start, end = membership.window_of(ny(2026, 10, 20))
        self.assertEqual(start, ny(2026, 10, 9, 0, 0, 0))
        self.assertEqual(end, ny(2026, 11, 9, 0, 0, 0))
        self.assertLess(start, end)
        self.assertLess(start.astimezone(UTC), end.astimezone(UTC))

    def test_is_active_correct_immediately_around_spring_forward_transition(self):
        expires_at = ny(2026, 3, 9, 0, 0, 0)
        # 2am-3am on 2026-03-08 does not exist in NY wall-clock time; pick a
        # safely-before instant instead (1:30am, before the 2am jump).
        just_before = ny(2026, 3, 8, 1, 30, 0)
        self.assertTrue(membership.is_active(expires_at, just_before))


if __name__ == "__main__":
    unittest.main()
