import sys
import types
import unittest

sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault(
    "playwright.sync_api",
    types.SimpleNamespace(sync_playwright=lambda: None, TimeoutError=TimeoutError),
)

from quantcheck import picks_email
from quantcheck.diff import compare
from quantcheck.picks_format import display_date, format_fetched, parse_date, parse_gt_score, parse_money, parse_pct, short_date, stock_count


def row(symbol, **extra):
    base = {"symbol": symbol, "company": f"{symbol} Inc.", "gt_score": "4.50/5", "analyst_signal": "Buy +0.30"}
    base.update(extra)
    return base


def snapshot(monthly, weekly, monthly_date="Updated October 1", weekly_date="Updated on Oct 2, 2026"):
    return {
        "fetched_at": "2026-10-06T12:30:54",
        "monthly": {"pick_date": monthly_date, "rows": monthly},
        "weekly": {"pick_date": weekly_date, "kind": "watchlist", "rows": weekly},
    }


class SubjectTests(unittest.TestCase):
    def test_portfolio_rebalance_lists_added_and_removed_symbols(self):
        old = snapshot([row("HPE"), row("PANW"), row("DELL")], [row("TXG")], monthly_date="Updated September 1")
        new = snapshot([row("MRNA"), row("VEEV"), row("DELL")], [row("TXG")])
        subject = picks_email.build_subject(compare(old, new), new)
        self.assertEqual(subject, "Quant GT · Portfolio rebalance: +MRNA +VEEV -HPE -PANW")

    def test_weekly_rotation_uses_watchlist_name(self):
        old = snapshot([row("DELL")], [row("TXG"), row("HALO")], weekly_date="Updated on Sep 25, 2026")
        new = snapshot([row("DELL")], [row("TXG"), row("U")])
        self.assertEqual(picks_email.build_subject(compare(old, new), new), "Quant GT · Weekly Watchlist: +U -HALO")

    def test_both_lists_changing_mentions_both(self):
        old = snapshot([row("A")], [row("B")], monthly_date="Updated September 1", weekly_date="Updated on Sep 25, 2026")
        new = snapshot([row("C")], [row("D")])
        subject = picks_email.build_subject(compare(old, new), new)
        self.assertEqual(subject, "Quant GT · Portfolio rebalance: +C -A | Weekly Watchlist: +D -B")

    def test_long_symbol_lists_are_truncated(self):
        old = snapshot([row(s) for s in "ABCDEF"], [], monthly_date="Updated September 1")
        new = snapshot([row(s) for s in "GHIJKL"], [])
        subject = picks_email.build_subject(compare(old, new), new)
        self.assertIn("+G +H +I +J +2 more", subject)
        self.assertLess(len(subject), 120)

    def test_single_signal_change_shows_old_and_new_values(self):
        old = snapshot([], [row("U", analyst_signal="Sell -0.20")])
        new = snapshot([], [row("U", analyst_signal="Strong Buy +0.60")])
        self.assertEqual(
            picks_email.build_subject(compare(old, new), new),
            "Quant GT · Analyst consensus: U Sell -0.20 → Strong Buy +0.60",
        )

    def test_same_signal_change_in_both_lists_counts_once(self):
        old = snapshot([row("U", analyst_signal="Sell -0.20")], [row("U", analyst_signal="Sell -0.20"), row("V", analyst_signal="Buy +0.10")])
        new = snapshot([row("U", analyst_signal="Buy +0.60")], [row("U", analyst_signal="Buy +0.60"), row("V", analyst_signal="Buy +0.90")])
        self.assertEqual(picks_email.build_subject(compare(old, new), new), "Quant GT · Analyst consensus changes: U, V")

    def test_date_only_refresh(self):
        old = snapshot([row("A")], [row("B")], weekly_date="Updated on Sep 25, 2026")
        new = snapshot([row("A")], [row("B")])
        self.assertEqual(
            picks_email.build_subject(compare(old, new), new),
            "Quant GT · Weekly Watchlist refreshed for Oct 2, 2026, no stock changes",
        )

    def test_gt_score_only_change(self):
        old = snapshot([], [row("A", gt_score="4.10/5"), row("B", gt_score="4.20/5")])
        new = snapshot([], [row("A", gt_score="4.30/5"), row("B", gt_score="4.00/5")])
        self.assertEqual(picks_email.build_subject(compare(old, new), new), "Quant GT · GT Score updated: A B")

    def test_no_diff_is_current_picks(self):
        self.assertEqual(picks_email.build_subject(None, snapshot([], [])), "Quant GT · Current picks")


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.old = snapshot([row("HPE", **{"return": "+18.90%"}), row("DELL")], [row("TXG", gt_score="4.79/5")], monthly_date="Updated September 1")
        self.new = snapshot([row("MRNA", sector="Health Technology"), row("DELL")], [row("TXG", gt_score="4.81/5")])
        self.diff = compare(self.old, self.new)

    def test_html_uses_previous_snapshot_for_removed_stock_details(self):
        html = picks_email.build_html(self.new, self.diff, previous=self.old)
        self.assertIn("HPE Inc.", html)
        self.assertIn("Last return", html)
        self.assertIn("+18.90%", html)
        self.assertIn(">NEW<", html)  # added stock flagged in the holdings list
        self.assertIn("TXG</b>", html)  # GT re-rating chip
        self.assertIn("Portfolio rebalanced", html)

    def test_html_preheader_carries_the_summary(self):
        html = picks_email.build_html(self.new, self.diff, previous=self.old)
        self.assertIn("display:none", html)
        self.assertIn("Portfolio rebalance: +MRNA -HPE", html)

    def test_banner_is_rendered_only_when_given(self):
        self.assertNotIn("Admin test", picks_email.build_html(self.new, self.diff))
        self.assertIn("Admin test", picks_email.build_html(self.new, self.diff, banner="Admin test"))

    def test_html_escapes_scraped_text(self):
        new = snapshot([row("X", company="<script>alert(1)</script>")], [])
        html = picks_email.build_html(new, None)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_text_body_lists_changes_and_changed_section_first(self):
        old = snapshot([row("DELL")], [row("TXG"), row("HALO")], weekly_date="Updated on Sep 25, 2026")
        new = snapshot([row("DELL")], [row("TXG"), row("U")])
        text = picks_email.build_text(new, compare(old, new), previous=old)
        self.assertIn("Changes:", text)
        self.assertIn("+ Added U", text)
        self.assertIn("- Removed HALO", text)
        self.assertLess(text.index("Weekly Watchlist — Oct 2, 2026 · 2 stocks"), text.index("Portfolio — October 1 · 1 stock"))

    def test_hidden_scrape_metadata_never_reaches_subscribers(self):
        diff = {
            "changed": True,
            "monthly": {"changed_flag": False},
            "weekly": {"changed_flag": True, "added": [], "removed": [], "changed": [
                {"symbol": "A", "fields": {"source_kind": {"old": "", "new": "watchlist"}}},
            ]},
        }
        new = snapshot([], [row("A")])
        self.assertNotIn("watchlist</span>", picks_email.build_html(new, diff))
        self.assertNotIn("Source", picks_email.build_text(new, diff))


class FormatTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(parse_money("$1,915.92"), 1915.92)
        self.assertEqual(parse_money("$81.10B"), 81.10e9)
        self.assertEqual(parse_money("$616.91M"), 616.91e6)
        self.assertIsNone(parse_money("—"))
        self.assertAlmostEqual(parse_pct("+8.90%"), 0.089)
        self.assertAlmostEqual(parse_pct("-27.62%"), -0.2762)
        self.assertEqual(parse_gt_score("4.61/5"), 4.61)
        self.assertEqual(str(parse_date("Oct 29, 2026")), "2026-10-29")
        self.assertEqual(str(parse_date("2026-10-01")), "2026-10-01")
        self.assertIsNone(parse_date("soon"))

    def test_display_helpers(self):
        self.assertEqual(display_date("Updated on Oct 2, 2026"), "Oct 2, 2026")
        self.assertEqual(display_date("Updated October 1"), "October 1")
        self.assertEqual(short_date("Oct 29, 2026"), "Oct 29")
        self.assertEqual(stock_count(1), "1 stock")
        self.assertEqual(stock_count(5), "5 stocks")

    def test_fetched_at_is_naive_utc_shown_in_et_and_beijing(self):
        self.assertEqual(format_fetched({"fetched_at": "2026-10-06T12:30:54"}), "Oct 6, 2026 · 08:30 ET / 20:30 Beijing")
        self.assertEqual(format_fetched({}), "")


if __name__ == "__main__":
    unittest.main()


class WatchlistContextTests(unittest.TestCase):
    def _row(self, **extra):
        base = row("TEAM", current_price="$196.50", signal_price="$187.63", signal_date="2026-10-05",
                   signal_at="2026-10-05T13:30:00Z", watch_reason="Gaining on its sector")
        base.update(extra)
        return base

    def test_since_signal_waits_for_the_signal_time(self):
        from datetime import datetime, timezone
        from quantcheck.picks_format import since_signal
        r = self._row()
        self.assertIsNone(since_signal(r, datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)))
        self.assertAlmostEqual(since_signal(r, datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)), 196.50 / 187.63 - 1)
        self.assertIsNone(since_signal(self._row(signal_price="")))

    def test_watchlist_rows_show_reason_and_move_after_signal(self):
        data = snapshot([], [self._row()])
        data["fetched_at"] = "2026-10-07T13:00:00"
        html = picks_email.build_html(data, None)
        self.assertIn("Gaining on its sector", html)
        self.assertIn("+4.7%", html)
        self.assertIn("since Oct 5", html)
        text = picks_email.build_text(data, None)
        self.assertIn("+4.7% since Oct 5", text)
        self.assertIn("Gaining on its sector", text)

    def test_weekend_detection_before_signal_shows_no_move(self):
        data = snapshot([], [self._row()])
        data["fetched_at"] = "2026-10-04T16:00:00"
        html = picks_email.build_html(data, None)
        self.assertNotIn("since Oct 5", html)
        self.assertIn("Gaining on its sector", html)

    def test_watchlist_context_fields_never_trigger_alerts(self):
        old = snapshot([], [self._row(signal_price="$180.00", watch_reason="", signal_at="", signal_date="")])
        new = snapshot([], [self._row()])
        self.assertFalse(compare(old, new)["changed"])
