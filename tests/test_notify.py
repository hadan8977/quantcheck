import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from quantcheck.gmail_api_notify import parse_recipients, refresh_gmail_credentials, send_email, send_email_per_recipient, send_via_gmail_api
from quantcheck.notify_routes import EmailRoute, admin_recipients, recipients_for_route, subscriber_recipients


class RecipientTests(unittest.TestCase):
    def test_parse_recipients_splits_dedupes_and_merges_file_with_inline_values(self):
        self.assertEqual(
            parse_recipients("a@example.com; b@example.com, a@example.com", file_path=""),
            ["a@example.com", "b@example.com"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            recipient_file = Path(tmp) / "notify_recipients.txt"
            recipient_file.write_text(
                "# primary recipients\na@example.com\nb@example.com, c@example.com\na@example.com\n\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_recipients("inline@example.com", file_path=recipient_file),
                ["inline@example.com", "a@example.com", "b@example.com", "c@example.com"],
            )

    def test_routes_admin_never_falls_back_to_subscribers_and_files_are_separate(self):
        env = {"NOTIFY_EMAIL_TO": "friend@example.com", "NOTIFY_EMAIL_FILE": "", "NOTIFY_ADMIN_EMAIL_TO": "admin@example.com, friend@example.com", "NOTIFY_ADMIN_EMAIL_FILE": ""}
        self.assertEqual(recipients_for_route(EmailRoute.PICKS_UPDATE, env), ["friend@example.com", "admin@example.com"])

        no_admins = {**env, "NOTIFY_ADMIN_EMAIL_TO": ""}
        self.assertEqual(recipients_for_route(EmailRoute.ADMIN, no_admins), [])
        with patch.dict("os.environ", {"NOTIFY_EMAIL_TO": "friend@example.com", "NOTIFY_EMAIL_FILE": ""}, clear=True):
            self.assertEqual(recipients_for_route(EmailRoute.ADMIN, {}), [])

        with tempfile.TemporaryDirectory() as tmp:
            subscriber_file = Path(tmp) / "subscribers.txt"
            admin_file = Path(tmp) / "admins.txt"
            subscriber_file.write_text("friend@example.com\n", encoding="utf-8")
            admin_file.write_text("admin@example.com\n", encoding="utf-8")
            files = {"NOTIFY_EMAIL_TO": "", "NOTIFY_EMAIL_FILE": str(subscriber_file), "NOTIFY_ADMIN_EMAIL_TO": "", "NOTIFY_ADMIN_EMAIL_FILE": str(admin_file)}

            self.assertEqual(subscriber_recipients(files), ["friend@example.com"])
            self.assertEqual(admin_recipients(files), ["admin@example.com"])
            self.assertEqual(recipients_for_route(EmailRoute.PICKS_UPDATE, files), ["friend@example.com", "admin@example.com"])


class SendEmailTests(unittest.TestCase):
    def test_one_private_message_per_recipient_via_brevo_without_gmail_or_smtp(self):
        calls = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            calls.append(list(to or []))
            return True

        # Regression guard: these tests must never append fixture recipients to the real
        # production logs/email_delivery_ledger.jsonl (docs/SITE_CHANGE_REPAIR.md A5), hence _ledger_record is mocked.
        with patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify.send_via_smtp") as smtp, \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            self.assertTrue(send_email("Subject", "Body", to=["a@example.com", "b@example.com"]))
        self.assertEqual(calls, [["a@example.com"], ["b@example.com"]])
        smtp.assert_not_called()
        self.assertEqual(ledger.call_count, 2)

        calls.clear()
        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify.send_via_gmail_api") as gmail, \
             patch("quantcheck.gmail_api_notify.send_via_smtp") as smtp, \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient("Subject", "Body", to=["a@example.com", "b@example.com"])
        self.assertEqual((delivered, failed), (["a@example.com", "b@example.com"], []))
        self.assertEqual(calls, [["a@example.com"], ["b@example.com"]])
        self.assertEqual(ledger.call_count, 2)
        ledger.assert_any_call("brevo", "Subject", "a@example.com", True, message_id=None)
        ledger.assert_any_call("brevo", "Subject", "b@example.com", True, message_id=None)
        gmail.assert_not_called()
        smtp.assert_not_called()

    def test_per_recipient_delivery_is_bounded_parallel(self):
        # Two workers must be inside the provider call at the same time: a barrier proves real
        # concurrency without sleeping (a serial implementation would time out and fail).
        barrier = threading.Barrier(2, timeout=10)
        calls = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            barrier.wait()
            calls.append(list(to or []))
            return True

        recipients = [f"user{i}@example.com" for i in range(8)]
        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo", "QUANTCHECK_EMAIL_WORKERS": "4"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient("Subject", "Body", to=recipients)

        self.assertEqual((delivered, failed), (recipients, []))
        self.assertEqual(len(calls), 8)
        self.assertEqual(ledger.call_count, 8)

    def test_per_recipient_delivery_retries_a_transient_provider_failure(self):
        attempts = []

        def fake_brevo(subject, body, to=None, attachments=None, html=None):
            attempts.append(list(to or []))
            return len(attempts) > 1

        with patch.dict("os.environ", {"EMAIL_PROVIDER": "brevo", "QUANTCHECK_EMAIL_WORKERS": "1"}, clear=True), \
             patch("quantcheck.gmail_api_notify.send_via_brevo_api", side_effect=fake_brevo), \
             patch("quantcheck.gmail_api_notify._ledger_record") as ledger:
            delivered, failed = send_email_per_recipient("Subject", "Body", to=["a@example.com"], retries=1)

        self.assertEqual(attempts, [["a@example.com"], ["a@example.com"]])
        self.assertEqual((delivered, failed), (["a@example.com"], []))
        ledger.assert_called_once_with("brevo", "Subject", "a@example.com", True, message_id=None)


class GmailCredentialTests(unittest.TestCase):
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
            env = {"GMAIL_API_ENABLED": "1", "GMAIL_API_TOKEN": str(token_path), "GMAIL_API_FROM": "sender@example.com"}
            with patch.dict("os.environ", env, clear=True), \
                 patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=FakeCreds()), \
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
                creds = refresh_gmail_credentials(token_path, ["scope-a"])

            backups = list((token_path.parent / "backup").glob("token.pre_refresh.*.json"))
            self.assertTrue(creds.valid)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"access_token":"new"}')
            self.assertTrue(backups)


if __name__ == "__main__":
    unittest.main()
