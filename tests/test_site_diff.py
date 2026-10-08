import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import unittest

from quantcheck.site_diff_notify import diff, screenshot_attachments


class SiteDiffTests(unittest.TestCase):
    def test_capture_failure_suppresses_site_change_alert(self):
        old = {"pages": [{"name": "dashboard", "headings": ["Old"], "nav": [], "buttons": [], "links": []}]}
        new = {"pages": [{"name": "dashboard", "capture_warning": "Timeout"}]}

        self.assertEqual(diff(old, new), [])

    def test_market_tools_noise_is_suppressed(self):
        old = {"pages": [{"name": "market_tools", "headings": ["Tools"], "nav": [], "buttons": [], "links": []}]}
        new = {
            "pages": [
                {
                    "name": "market_tools",
                    "headings": ["Tools", "Breaking news"],
                    "nav": [],
                    "buttons": [],
                    "links": [{"text": "News", "href": "https://www.cnbc.com/story"}],
                }
            ]
        }

        self.assertEqual(diff(old, new), [])


    def test_news_ticker_links_are_suppressed_but_real_nav_change_surfaces(self):
        old = {
            "pages": [
                {
                    "name": "dashboard",
                    "headings": [],
                    "nav": ["Research NEW"],
                    "buttons": [],
                    "links": [
                        {"text": "Research NEW", "href": "https://quantgt.io/research"},
                        {"text": "BofA Raises SanDisk (SNDK) Price Target 11h ago", "href": "https://finance.yahoo.com/markets/stocks/articles/x"},
                    ],
                }
            ]
        }
        new = {
            "pages": [
                {
                    "name": "dashboard",
                    "headings": [],
                    "nav": ["Quant Research NEW"],
                    "buttons": [],
                    "links": [
                        {"text": "Quant Research NEW", "href": "https://quantgt.io/research"},
                        {"text": "Dow Jones Futures: Stock Market Jumps 7m ago", "href": "https://finance.yahoo.com/m/abc"},
                    ],
                }
            ]
        }

        lines = diff(old, new)
        # The genuine nav rename must surface.
        self.assertIn("dashboard nav added: Quant Research NEW", lines)
        self.assertIn("dashboard nav removed: Research NEW", lines)
        # No news-ticker link (Yahoo Finance / "N ago" timestamps) may appear anywhere.
        joined = "\n".join(lines)
        self.assertNotIn("finance.yahoo.com", joined)
        self.assertNotIn("ago", joined)

    def test_existing_screenshots_become_email_attachments(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            dash = Path(tmp) / "dashboard.png"
            missing = Path(tmp) / "missing.png"
            dash.write_bytes(b"png")
            snapshot = {"screenshots": {"dashboard": str(dash), "weekly": str(missing)}}

            self.assertEqual(screenshot_attachments(snapshot), [dash])


if __name__ == "__main__":
    unittest.main()


def member_page(name, headings=(), nav=("Portfolio", "Weekly Digest"), buttons=(), links=()):
    return {"name": name, "headings": list(headings), "nav": list(nav), "buttons": list(buttons),
            "links": [{"text": t, "href": h} for t, h in links]}


def member_snapshot(*pages):
    return {"capture_mode": "fresh_login_member", "pages": list(pages)}


class MemberPageNoiseTests(unittest.TestCase):
    def week(self, cards, pct, headline, archive, article):
        return member_snapshot(
            member_page("weekly", ["Watchlist"], buttons=["U", *cards]),
            member_page("track_record", ["Track Record", "Monthly returns"], buttons=["U", *pct]),
            member_page("weekly_digest", ["Weekly Digest", headline, "Sector rotation"],
                        buttons=["U", "Browse the archive", *archive],
                        links=[("01 The news", "https://quantgt.io/weekly-digest#digest-take"),
                               ("Some headline", "https://www.cnn.com/2026/10/01/story")]),
            member_page("research", ["Quant Research", article], buttons=["U", "ALL 12" if "1%" in article else "ALL 13"],
                        links=[(article, "https://quantgt.io/research/" + article[:5])]),
        )

    def test_weekly_data_rotation_is_not_a_site_change(self):
        old = self.week(["TXG 10x Genomics, Inc. Health Technology"], ["+17.4%", "+2.1%"], "Hiring stalled",
                        ["SEP 25 Bond yields hit 5%"], "Why Are Only 1% of Day Traders Profitable?")
        new = self.week(["NVDA NVIDIA Corporation Electronic Technology"], ["+17.4%", "+3.0%"], "Fed cuts rates",
                        ["OCT 2 Hiring stalled"], "A brand new article")
        self.assertEqual(diff(old, new), [])

    def test_real_structure_changes_still_surface(self):
        base = self.week(["TXG 10x Genomics"], ["+1.0%"], "H", ["SEP 25 X"], "Why Are Only 1% of Day Traders Profitable?")
        changed = self.week(["TXG 10x Genomics"], ["+1.0%"], "H", ["SEP 25 X"], "Why Are Only 1% of Day Traders Profitable?")
        changed["pages"][0]["buttons"].append("Export CSV")                       # new watchlist feature
        changed["pages"][2]["links"].pop(0)                                       # digest section removed
        changed["pages"][3]["nav"] = ["Portfolio"]                                # nav item removed
        lines = diff(base, changed)
        self.assertTrue(any("weekly buttons added: Export CSV" in l for l in lines), lines)
        self.assertTrue(any("weekly_digest links removed" in l for l in lines), lines)
        self.assertTrue(any("research nav removed: Weekly Digest" in l for l in lines), lines)

    def test_switch_from_placeholder_capture_is_a_baseline_not_an_alert(self):
        placeholder = {"pages": [member_page("weekly", ["Watchlist"], buttons=["U", "Subscribe", "SN SNDK Sandisk Holdings Technology"])]}
        member = member_snapshot(member_page("weekly", ["Watchlist", "Portfolio candidate"], buttons=["U", "TXG 10x Genomics"]),
                                 member_page("live_update", ["Live Update"], buttons=["All", "Live Insights"]))
        self.assertEqual(diff(placeholder, member), [])


class SiteSnapshotAccessTests(unittest.TestCase):
    def test_member_access_requires_active_subscription(self):
        import sys
        import types
        sys.modules.setdefault("playwright", types.ModuleType("playwright"))
        sys.modules.setdefault("playwright.sync_api", types.SimpleNamespace(sync_playwright=lambda: None, TimeoutError=TimeoutError))
        from quantcheck.site_snapshot import PAGES, member_access

        def page(result=None, exc=None):
            def evaluate(script):
                if exc:
                    raise exc
                return result
            return types.SimpleNamespace(evaluate=evaluate)

        self.assertTrue(member_access(page({"subscription": {"hasAccess": True, "status": "active"}})))
        self.assertFalse(member_access(page({"subscription": None})))
        self.assertFalse(member_access(page(None)))
        self.assertFalse(member_access(page(exc=RuntimeError("network"))))
        names = {name for name, _ in PAGES}
        self.assertTrue({"live_update", "track_record", "weekly_digest", "research"} <= names)
        self.assertFalse(any("/dashboard" in url for _, url in PAGES))
