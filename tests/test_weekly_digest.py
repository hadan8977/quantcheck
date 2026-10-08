import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault(
    "playwright.sync_api",
    types.SimpleNamespace(sync_playwright=lambda: None, TimeoutError=TimeoutError),
)

from quantcheck import picks_check, picks_report, weekly_digest
from quantcheck.notify_routes import EmailRoute


def digest(week="2026-10-02", **extra):
    d = {
        "week_start": week,
        "headline": "Hiring stalled at 29,000 jobs",
        "sections": {
            "summary": {"body": "The job market weakened."},
            "stats": [{"tone": "down", "label": "S&P 500", "value": "−0.2%"}],
            "fear_greed": {"label": "Fear", "value": 31, "prev_week_label": "Fear", "prev_week_value": 37},
            "market_news": [{"title": "Jobs <b>miss</b>", "source": "CNBC", "note": "Few jobs.", "sources": [{"url": "https://www.cnbc.com/x"}]}],
            "rotation": {"body": "Para one.\n\nPara two.", "benchmark_change_pct": -0.22,
                         "points": [{"short": "Semiconductors", "ticker": "SMH", "quadrant": "Weakening", "week_change_pct": 3.96}]},
            "monthly": [{"ticker": "MRNA", "week_change_pct": -4.46, "body": "Citi downgraded Moderna."}],
            "watchlist": [{"ticker": "TXG", "name": "10x Genomics", "score": 4.81, "reason": "At a 52-week high"}],
            "tradingview": [{"ticker": "IWM", "direction": "long", "signal_date": "2026-10-01", "gain_since_signal_pct": 58.18}],
            "earnings": [{"date": "2026-10-08", "total": 3, "symbols": ["PEP"]}],
        },
    }
    d.update(extra)
    return d


class RenderTests(unittest.TestCase):
    def test_subject_and_sections(self):
        d = digest()
        self.assertEqual(weekly_digest.build_subject(d), "Quant GT · Weekly Digest: Hiring stalled at 29,000 jobs")
        html = weekly_digest.build_html(d)
        for fragment in ("Week of Oct 2, 2026", "The news that moved the market", "Sector rotation", "Para two.",
                         "Portfolio holdings this week", "Citi downgraded Moderna.", "-4.5% this week",
                         "At a 52-week high", "Signal highlights", "since Oct 1", "Earnings next week", "PEP +2 more",
                         "31 Fear", "https://www.cnbc.com/x"):
            self.assertIn(fragment, html)
        self.assertNotIn("<b>miss</b>", html)  # scraped text is escaped
        text = weekly_digest.build_text(d)
        self.assertIn("THE NEWS", text)
        self.assertIn("TXG 10x Genomics | GT 4.81 | At a 52-week high", text)

    def test_validate_rejects_locked_or_incomplete(self):
        with self.assertRaisesRegex(weekly_digest.DigestError, "locked"):
            weekly_digest.validate(digest(locked=True))
        bad = digest()
        bad["sections"]["watchlist"] = []
        with self.assertRaisesRegex(weekly_digest.DigestError, "watchlist"):
            weekly_digest.validate(bad)
        with self.assertRaisesRegex(weekly_digest.DigestError, "week_start"):
            weekly_digest.validate(digest(week="soon"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        self.state_file = state / "weekly_digest_state.json"
        self.sent = []
        self.patches = [
            patch.object(picks_check, "STATE", state),
            patch.object(picks_check, "log"),
            patch.object(picks_check, "send_email", side_effect=self._send),
            patch.object(weekly_digest, "_now", return_value=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)),
        ]
        for p in self.patches:
            p.start()
        self.fail_send = False

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _send(self, subject, body, media, html_body=None, route=EmailRoute.PICKS_UPDATE):
        self.sent.append((subject, route))
        if self.fail_send and route == EmailRoute.PICKS_UPDATE:
            raise RuntimeError("email delivery failed after retry for 2 recipient(s)")
        return ["a@example.com", "b@example.com"], []

    def _run(self, d):
        return weekly_digest.run({}, fetch=lambda env: d)

    def _state(self):
        return json.loads(self.state_file.read_text())

    def test_first_run_only_records_a_baseline(self):
        self.assertEqual(self._run(digest())["status"], "baseline")
        self.assertEqual(self.sent, [])
        self.assertEqual(self._run(digest())["status"], "already_handled")
        self.assertEqual(self.sent, [])

    def test_new_week_is_sent_exactly_once(self):
        self._run(digest("2026-09-25"))
        result = self._run(digest("2026-10-02"))
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.sent, [("Quant GT · Weekly Digest: Hiring stalled at 29,000 jobs", EmailRoute.PICKS_UPDATE)])
        self.assertEqual(self._state()["weeks"]["2026-10-02"]["status"], "sent")
        self._run(digest("2026-10-02"))
        self.assertEqual(len(self.sent), 1)

    def test_failed_send_is_not_retried_and_alerts_admins(self):
        self._run(digest("2026-09-25"))
        self.fail_send = True
        with self.assertRaises(RuntimeError):
            self._run(digest("2026-10-02"))
        self.assertEqual(self._state()["weeks"]["2026-10-02"]["status"], "failed")
        self.assertEqual([r for _, r in self.sent], [EmailRoute.PICKS_UPDATE, EmailRoute.ADMIN])
        self.fail_send = False
        self.assertEqual(self._run(digest("2026-10-02"))["status"], "already_handled")
        self.assertEqual(len(self.sent), 2)

    def test_crash_mid_send_leaves_sending_status_that_blocks_resend(self):
        self._run(digest("2026-09-25"))
        state = self._state()
        state["weeks"]["2026-10-02"] = {"status": "sending"}
        self.state_file.write_text(json.dumps(state))
        self.assertEqual(self._run(digest("2026-10-02"))["status"], "already_handled")
        self.assertEqual(self.sent, [])

    def test_stale_digest_is_skipped(self):
        self._run(digest("2026-08-28"))
        self.assertEqual(self._run(digest("2026-09-18"))["status"], "skipped_stale")
        self.assertEqual(self.sent, [])

    def test_invalid_digest_alerts_admins_without_touching_subscribers(self):
        self._run(digest("2026-09-25"))
        with self.assertRaises(weekly_digest.DigestError):
            self._run(digest("2026-10-02", locked=True))
        self.assertEqual([r for _, r in self.sent], [EmailRoute.ADMIN])


class ScraperContextTests(unittest.TestCase):
    def test_merge_adds_signal_price_and_time_from_weekly_api(self):
        rows = [{"symbol": "TEAM", "company": "Atlassian", "sector": "Tech", "current_price": "$196.50"}]
        api = [{"ticker": "TEAM", "score": 4.62, "price": 187.63, "sell_price": 196.495, "signal_ts": "2026-10-05T13:30:00Z"}]
        merged = picks_report.merge_watchlist_api_scores(rows, api)[0]
        self.assertEqual(merged["signal_price"], "$187.63")
        self.assertEqual(merged["signal_date"], "2026-10-05")
        self.assertEqual(merged["signal_at"], "2026-10-05T13:30:00Z")
        self.assertEqual(merged["current_price"], "$196.50")  # scraped live price is kept

    def _page(self, payload=None, exc=None):
        page = types.SimpleNamespace()
        def evaluate(script):
            if exc:
                raise exc
            return payload
        page.evaluate = evaluate
        return page

    def test_digest_reasons_attach_only_for_the_same_week(self):
        api = [{"ticker": "TXG", "week_start": "2026-10-02"}]
        body = {"week_start": "2026-10-02", "sections": {"watchlist": [{"ticker": "TXG", "reason": "At a 52-week high"}]}}
        rows = picks_report.attach_digest_reasons(self._page({"status": 200, "body": body}), [{"symbol": "TXG"}], api)
        self.assertEqual(rows[0]["watch_reason"], "At a 52-week high")
        stale = dict(body, week_start="2026-09-25")
        rows = picks_report.attach_digest_reasons(self._page({"status": 200, "body": stale}), [{"symbol": "TXG"}], api)
        self.assertNotIn("watch_reason", rows[0])

    def test_digest_reason_failures_never_break_the_scrape(self):
        rows = [{"symbol": "TXG"}]
        self.assertEqual(picks_report.attach_digest_reasons(self._page(exc=RuntimeError("boom")), rows, []), rows)
        self.assertEqual(picks_report.attach_digest_reasons(self._page({"status": 403, "body": None}), rows, []), rows)


if __name__ == "__main__":
    unittest.main()
