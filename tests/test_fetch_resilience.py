import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import copy
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault(
    "playwright.sync_api",
    types.SimpleNamespace(sync_playwright=lambda: None, TimeoutError=TimeoutError),
)

from quantcheck import picks_check  # noqa: E402
from quantcheck.notify_routes import EmailRoute  # noqa: E402


def weekly_row(symbol="W1"):
    return {
        "symbol": symbol,
        "company": "Weekly One Inc.",
        "current_price": "$10.00",
        "buy_or_entry_price": "$9.50",
        "sector": "Technology",
        "gt_score": "4.20/5",
        "next_earnings": "2026-06-01",
        "analyst_signal": "Buy +0.20",
    }


VALID_DATA = {
    "fetched_at": "2026-05-27T14:30:00",
    "source": "https://quantgt.io",
    "monthly": {
        "pick_date": "May Holdings 05/01/26 - now",
        "rows": [
            {
                "symbol": "M1",
                "company": "Monthly One Inc.",
                "current_price": "$20.00",
                "return": "+12.30%",
                "sector": "Technology",
                "gt_score": "4.50/5",
                "buy_or_entry_price": "$18.00",
                "next_earnings": "2026-06-15",
                "analyst_signal": "Buy +0.25",
            }
        ],
    },
    "weekly": {
        "pick_date": "Week of May 25, 2026",
        "rows": [weekly_row(f"W{i}") for i in range(1, 11)],
    },
}


class FetchResilienceTests(unittest.TestCase):
    def test_fetch_current_retries_transient_failed_capture_before_returning_data(self):
        attempts = []

        def flaky_fetch():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("logged-in monthly picks validation failed: no monthly rows captured")
            return VALID_DATA

        with patch.object(picks_check.report, "fetch", side_effect=flaky_fetch), \
             patch.object(picks_check, "log"), \
             patch.object(picks_check, "json_dump"), \
             patch.object(picks_check, "prune_old_files"):
            data = picks_check.fetch_current(max_attempts=2, retry_delay_seconds=0)

        self.assertEqual(data["monthly"]["rows"][0]["symbol"], "M1")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(data["auth_verified"])

    def test_failures_notify_admin_route_with_card_html(self):
        # run_test_email and run_check share one contract: a failed fetch emails the admin route only.
        # capture_logged_in_screenshots is patched: unpatched it tries to launch a real browser (~17 s).
        sent = []

        def fake_notify(subject, body, media=None, html_body=None, telegram_body=None, route=EmailRoute.PICKS_UPDATE):
            sent.append({"subject": subject, "body": body, "html_body": html_body, "route": route, "media": media or []})

        def run_test_email():
            picks_check.run_test_email()

        def run_check():
            with patch.object(picks_check, "trading_day", return_value=True), \
                 patch.object(picks_check, "current_window", return_value="premarket_0830"):
                picks_check.run_check(force=False, no_random=True)

        cases = (("manual test email", run_test_email, "Quant GT Monitor Test Failed"), ("scheduled check", run_check, "Quant GT Monitor Failed"))
        for name, run, subject in cases:
            with self.subTest(name):
                sent.clear()
                with patch.object(picks_check, "fetch_current", side_effect=RuntimeError("monthly rows stayed empty after retries")), \
                     patch.object(picks_check, "capture_logged_in_screenshots", return_value={}), \
                     patch.object(picks_check, "notify", side_effect=fake_notify), \
                     patch.object(picks_check, "log"), \
                     patch.object(picks_check, "write_health"), \
                     patch.object(picks_check, "json_load", return_value={}):
                    with self.assertRaises(RuntimeError):
                        run()

                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["route"], EmailRoute.ADMIN)
                self.assertIn(subject, sent[0]["subject"])
                self.assertIn("monthly rows stayed empty", sent[0]["body"])
                self.assertIsNotNone(sent[0]["html_body"])
                self.assertIn("Quant GT Monitor", sent[0]["html_body"])
                self.assertIn("Error", sent[0]["html_body"])
                self.assertIn("monthly rows stayed empty", sent[0]["html_body"])

    def test_failure_alert_still_goes_out_when_the_failure_screenshot_also_fails(self):
        sent = []
        with patch.object(picks_check, "fetch_current", side_effect=RuntimeError("fetch broke")), \
             patch.object(picks_check, "capture_logged_in_screenshots", side_effect=RuntimeError("no browser")), \
             patch.object(picks_check, "notify", side_effect=lambda subject, body, media=None, **kw: sent.append((subject, media, kw["route"]))), \
             patch.object(picks_check, "log"), \
             patch.object(picks_check, "write_health"), \
             patch.object(picks_check, "json_load", return_value={}):
            with self.assertRaises(RuntimeError):
                picks_check.run_test_email()

        self.assertEqual([(subject, media, route) for subject, media, route in sent], [("Quant GT Monitor Test Failed", [], EmailRoute.ADMIN)])

    def test_manual_test_email_success_resets_health_without_promoting_baseline(self):
        with patch.object(picks_check, "fetch_current", return_value=VALID_DATA), \
             patch.object(picks_check, "json_dump"), \
             patch.object(picks_check.report, "export_excel", return_value=picks_check.OUTPUT / "test.xlsx"), \
             patch.object(picks_check, "capture_logged_in_screenshots", return_value={}), \
             patch.object(picks_check, "notify"), \
             patch.object(picks_check, "write_health") as write_health, \
             redirect_stdout(io.StringIO()) as printed:
            picks_check.run_test_email()

        self.assertEqual(json.loads(printed.getvalue())["status"], "test_notification_sent")
        write_health.assert_called_once()
        health = write_health.call_args.kwargs
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error"])
        self.assertEqual(health["last_window"], "manual_test_email")
        self.assertEqual(health["monthly_date"], VALID_DATA["monthly"]["pick_date"])
        self.assertEqual(health["weekly_date"], VALID_DATA["weekly"]["pick_date"])

    def test_run_check_change_sends_informative_subject_with_diff_aware_excel(self):
        old = copy.deepcopy(VALID_DATA)
        new = copy.deepcopy(VALID_DATA)
        new["weekly"]["rows"][0] = weekly_row("NEW1")
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            latest = root / "state" / "latest_picks.json"
            latest.write_text(json.dumps(old), encoding="utf-8")
            health = root / "state" / "health.json"
            health.write_text(json.dumps({"mode": "baseline"}), encoding="utf-8")
            with patch.object(picks_check, "ROOT", root), \
                 patch.object(picks_check, "STATE", root / "state"), \
                 patch.object(picks_check, "LATEST", latest), \
                 patch.object(picks_check, "PREVIOUS", root / "state" / "previous_picks.json"), \
                 patch.object(picks_check, "HEALTH", health), \
                 patch.object(picks_check, "LAST_CHANGE_NOTIFICATION", root / "state" / "last_change.json"), \
                 patch.object(picks_check, "trading_day", return_value=True), \
                 patch.object(picks_check, "current_window", return_value="premarket_0830"), \
                 patch.object(picks_check, "fetch_current", return_value=new), \
                 patch.object(picks_check, "prune_old_files"), \
                 patch.object(picks_check, "capture_logged_in_screenshots", return_value={}), \
                 patch.object(picks_check, "log"), \
                 patch.object(picks_check.report, "export_excel", return_value=root / "r.xlsx") as export_excel, \
                 patch.object(picks_check, "notify", side_effect=lambda subject, body, media=None, **kw: sent.append((subject, body, kw))):
                picks_check.run_check(force=False, no_random=True)
            final_health = json.loads(health.read_text(encoding="utf-8"))

        self.assertEqual(len(sent), 1)
        subject, body, kwargs = sent[0]
        self.assertEqual(subject, "Quant GT · Weekly Picks: +NEW1 -W1")
        self.assertIn("+ Added NEW1", body)
        self.assertIn("Weekly Picks updated", kwargs["html_body"])
        self.assertNotIn("window=", kwargs["html_body"])
        self.assertEqual(export_excel.call_args.kwargs["previous"]["weekly"]["rows"][0]["symbol"], "W1")
        self.assertTrue(export_excel.call_args.kwargs["diff"]["changed"])
        # a successful check run must clear the stale mode left by run_baseline
        self.assertEqual(final_health["mode"], "check")

    def test_send_email_raises_when_all_recipients_fail(self):
        with patch.object(picks_check, "load_env", return_value={"NOTIFY_EMAIL_TO": "a@example.com", "NOTIFY_EMAIL_FILE": "", "NOTIFY_ADMIN_EMAIL_TO": "", "NOTIFY_ADMIN_EMAIL_FILE": ""}), \
             patch.object(picks_check, "deliver_email", return_value=([], ["a@example.com"])), \
             patch.object(picks_check, "log"):
            with self.assertRaisesRegex(RuntimeError, "email delivery failed after retry for 1 recipient"):
                picks_check.send_email("Quant GT Picks Updated", "Body")

    def test_send_email_retries_partial_delivery_failures_before_returning(self):
        attempts = []

        def fake_deliver(subject, body, to=None, attachments=None, html=None):
            attempts.append(list(to or []))
            if len(attempts) == 1:
                return ["a@example.com"], ["b@example.com"]
            return ["b@example.com"], []

        with patch.object(picks_check, "load_env", return_value={"NOTIFY_EMAIL_TO": "a@example.com,b@example.com", "NOTIFY_EMAIL_FILE": "", "NOTIFY_ADMIN_EMAIL_TO": "", "NOTIFY_ADMIN_EMAIL_FILE": ""}), \
             patch.object(picks_check, "deliver_email", side_effect=fake_deliver), \
             patch.object(picks_check, "log") as log:
            delivered, failed = picks_check.send_email("Quant GT Picks Updated", "Body")

        self.assertEqual(attempts, [["a@example.com", "b@example.com"], ["b@example.com"]])
        self.assertEqual(delivered, ["a@example.com", "b@example.com"])
        self.assertEqual(failed, [])
        self.assertTrue(any("email retrying" in call.args[0] for call in log.call_args_list))

    def test_weekly_screenshot_ready_requires_full_top_10_before_capture(self):
        class FakePage:
            def __init__(self):
                self.wait_calls = []

            def wait_for_function(self, script, arg=None, timeout=None):
                self.wait_calls.append({"script": script, "arg": arg, "timeout": timeout})

            def wait_for_timeout(self, ms):
                self.wait_calls.append({"timeout_ms": ms})

        class PartialPage:
            def wait_for_function(self, *args, **kwargs):
                raise AssertionError("should not wait for screenshot when parsed rows are partial")

            def wait_for_timeout(self, ms):
                raise AssertionError("should not sleep when parsed rows are partial")

        page = FakePage()
        rows = [{"symbol": f"W{i}", "gt_score": "4.0/5"} for i in range(10)]

        with patch.object(picks_check.report, "wait_for_picks_content") as wait_content, \
             patch.object(picks_check.report, "wait_for_parsable_picks_rows", return_value=rows) as wait_rows:
            picks_check._wait_for_screenshot_ready(page, "weekly")

        wait_content.assert_called_once_with(page)
        wait_rows.assert_called_once_with(page, "weekly")
        self.assertEqual(page.wait_calls[0]["arg"], "weekly")
        self.assertEqual(page.wait_calls[0]["timeout"], 20000)
        self.assertIn("height > window.innerHeight + 400", page.wait_calls[0]["script"])
        self.assertEqual(page.wait_calls[1], {"timeout_ms": 1000})

        partial = [{"symbol": f"W{i}", "gt_score": "4.0/5"} for i in range(5)]
        with patch.object(picks_check.report, "wait_for_picks_content"), \
             patch.object(picks_check.report, "wait_for_parsable_picks_rows", return_value=partial):
            with self.assertRaisesRegex(RuntimeError, "expected 10 parsed rows"):
                picks_check._wait_for_screenshot_ready(PartialPage(), "weekly")


if __name__ == "__main__":
    unittest.main()
