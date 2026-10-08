import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from quantcheck.gmail_api_notify import parse_recipients, send_email, send_via_gmail_api, send_email_per_recipient
from quantcheck.notify_routes import EmailRoute, admin_recipients, recipients_for_route, subscriber_recipients


class NotifyTests(unittest.TestCase):
    def test_parse_recipients_splits_commas_and_semicolons(self):
        self.assertEqual(
            parse_recipients("a@example.com; b@example.com, a@example.com", file_path=""),
            ["a@example.com", "b@example.com"],
        )

    def test_parse_recipients_reads_file_and_merges_inline_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipient_file = Path(tmp) / "notify_recipients.txt"
            recipient_file.write_text(
                "# primary recipients\n"
                "a@example.com\n"
                "b@example.com, c@example.com\n"
                "a@example.com\n"
                "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                parse_recipients("inline@example.com", file_path=recipient_file),
                ["inline@example.com", "a@example.com", "b@example.com", "c@example.com"],
            )

    def test_picks_update_goes_to_subscribers_and_admins(self):
        env = {
            "NOTIFY_EMAIL_TO": "friend@example.com",
            "NOTIFY_EMAIL_FILE": "",
            "NOTIFY_ADMIN_EMAIL_TO": "admin@example.com, friend@example.com",
            "NOTIFY_ADMIN_EMAIL_FILE": "",
        }

        self.assertEqual(
            recipients_for_route(EmailRoute.PICKS_UPDATE, env),
            ["friend@example.com", "admin@example.com"],
        )

    def test_admin_route_never_falls_back_to_subscribers(self):
        env = {
            "NOTIFY_EMAIL_TO": "friend@example.com",
            "NOTIFY_EMAIL_FILE": "",
            "NOTIFY_ADMIN_EMAIL_TO": "",
            "NOTIFY_ADMIN_EMAIL_FILE": "",
        }

        self.assertEqual(recipients_for_route(EmailRoute.ADMIN, env), [])

    def test_admin_route_never_falls_back_to_environment_subscribers(self):
        with patch.dict("os.environ", {"NOTIFY_EMAIL_TO": "friend@example.com", "NOTIFY_EMAIL_FILE": ""}, clear=True):
            self.assertEqual(recipients_for_route(EmailRoute.ADMIN, {}), [])

    def test_admin_and_subscriber_files_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            subscriber_file = Path(tmp) / "subscribers.txt"
            admin_file = Path(tmp) / "admins.txt"
            subscriber_file.write_text("friend@example.com\n", encoding="utf-8")
            admin_file.write_text("admin@example.com\n", encoding="utf-8")
            env = {
                "NOTIFY_EMAIL_TO": "",
                "NOTIFY_EMAIL_FILE": str(subscriber_file),
                "NOTIFY_ADMIN_EMAIL_TO": "",
                "NOTIFY_ADMIN_EMAIL_FILE": str(admin_file),
            }

            self.assertEqual(subscriber_recipients(env), ["friend@example.com"])
            self.assertEqual(admin_recipients(env), ["admin@example.com"])
            self.assertEqual(
                recipients_for_route(EmailRoute.PICKS_UPDATE, env),
                ["friend@example.com", "admin@example.com"],
            )

    def test_send_email_sends_one_private_message_per_recipient(self):
        calls = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            calls.append(list(to or []))
            return True

        with patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify.send_via_smtp") as smtp, \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            self.assertTrue(send_email("Subject", "Body", to=["a@example.com", "b@example.com"]))

        self.assertEqual(calls, [["a@example.com"], ["b@example.com"]])
        smtp.assert_not_called()
        # Regression guard: this test must never append fixture recipients to
        # the real production logs/email_delivery_ledger.jsonl. See
        # docs/SITE_CHANGE_REPAIR.md A5 for the incident this repeats if it
        # regresses (same failure mode, different file).
        self.assertEqual(ledger.call_count, 2)

    def test_email_provider_brevo_bypasses_gmail_and_smtp(self):
        calls = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            calls.append(list(to or []))
            return True

        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify.send_via_gmail_api") as gmail, \
             patch("quantcheck.gmail_api_notify.send_via_smtp") as smtp, \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient("Subject", "Body", to=["a@example.com", "b@example.com"])

        self.assertEqual(delivered, ["a@example.com", "b@example.com"])
        self.assertEqual(failed, [])
        self.assertEqual(calls, [["a@example.com"], ["b@example.com"]])
        self.assertEqual(ledger.call_count, 2)
        ledger.assert_any_call("brevo", "Subject", "a@example.com", True, message_id=None)
        ledger.assert_any_call("brevo", "Subject", "b@example.com", True, message_id=None)
        gmail.assert_not_called()
        smtp.assert_not_called()

    def test_send_email_per_recipient_uses_bounded_parallel_delivery(self):
        active = 0
        max_active = 0
        lock = threading.Lock()
        calls = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append(list(to or []))
            time.sleep(0.02)
            with lock:
                active -= 1
            return True

        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo", "QUANTCHECK_EMAIL_WORKERS": "4"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient(
                "Subject",
                "Body",
                to=[f"user{i}@example.com" for i in range(8)],
            )

        self.assertEqual(delivered, [f"user{i}@example.com" for i in range(8)])
        self.assertEqual(failed, [])
        self.assertEqual(len(calls), 8)
        self.assertGreater(max_active, 1)
        self.assertEqual(ledger.call_count, 8)

    def test_send_email_per_recipient_retries_transient_provider_failure(self):
        attempts = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            attempts.append(list(to or []))
            return len(attempts) > 1

        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo", "QUANTCHECK_EMAIL_WORKERS": "1"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient("Subject", "Body", to=["a@example.com"], retries=1)

        self.assertEqual(attempts, [["a@example.com"], ["a@example.com"]])
        self.assertEqual(delivered, ["a@example.com"])
        self.assertEqual(failed, [])
        ledger.assert_called_once_with("brevo", "Subject", "a@example.com", True, message_id=None)

    def test_gmail_api_send_skips_refresh_when_credentials_are_valid(self):
        class FakeCreds:
            expired = False
            refresh_token = None
            valid = True

            def to_json(self):
                return "{}"

        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token.json"
            token_path.write_text("{}", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "GMAIL_API_ENABLED": "1",
                    "GMAIL_API_TOKEN": str(token_path),
                    "GMAIL_API_FROM": "sender@example.com",
                },
                clear=True,
            ), patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=FakeCreds()), \
                 patch("quantcheck.gmail_api_notify.refresh_gmail_credentials") as refresh:
                send_via_gmail_api("Subject", "Body", to=["admin@example.com"])

        refresh.assert_not_called()

    def test_refresh_helper_writes_atomically_and_backups(self):
        class FakeCreds:
            expired = True
            refresh_token = "refresh"
            valid = True

            def refresh(self, request):
                self.valid = True

            def to_json(self):
                return '{"access_token":"new"}'

        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token.json"
            token_path.write_text('{"access_token":"old"}', encoding="utf-8")
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=FakeCreds()):
                from quantcheck.gmail_api_notify import refresh_gmail_credentials
                creds = refresh_gmail_credentials(token_path, ["scope-a"])

            backups = list((token_path.parent / "backup").glob("token.pre_refresh.*.json"))
            self.assertTrue(creds.valid)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"access_token":"new"}')
            self.assertTrue(backups)


if __name__ == "__main__":
    unittest.main()
