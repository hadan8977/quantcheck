import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import fcntl
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from quantcheck.official_mail_forwarder import (
    OfficialMail,
    build_forward_body,
    forward_official_mail,
    gmail_messages_to_official_mails,
    main,
    matches_official_mail,
    official_mail_from_message,
    search_query,
    split_patterns,
)

MODULE = "quantcheck.official_mail_forwarder"


def official_message(subject="Monthly Picks Updated", date="Mon, 25 May 2026 08:00:00 +0000", text="New monthly picks are available."):
    msg = EmailMessage()
    msg["From"] = "Quant GT <support@quantgt.io>"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(text)
    return msg


class FakeImap:
    """IMAP double serving {uid: EmailMessage}."""

    def __init__(self, *messages):
        self.messages = {str(i): msg for i, msg in enumerate(messages, start=1)}

    def select(self, mailbox, readonly=True):
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(self.messages).encode()]
        if command == "fetch":
            uid = args[0]
            return "OK", [(f"{uid} (RFC822 {{1}}".encode(), self.messages[uid].as_bytes())]
        raise AssertionError(command)

    def logout(self):
        return "OK", []


def gmail_message(with_html=False):
    parts = [{"mimeType": "text/plain", "body": {"data": "TmV3IHBpY2tzIGFyZSBhdmFpbGFibGUu"}}]
    if with_html:
        parts.append({"mimeType": "text/html", "body": {"data": "PHA-TmV3IHBpY2tzIGFyZSBhdmFpbGFibGUuPC9wPg"}})
    return {
        "id": "abc123",
        "payload": {
            "headers": [
                {"name": "From", "value": "Quant GT <support@quantgt.io>"},
                {"name": "Subject", "value": "Monthly Picks Updated"},
                {"name": "Date", "value": "Mon, 25 May 2026 08:00:00 +0000"},
            ],
            "parts": parts,
        },
    }


class ParsingTests(unittest.TestCase):
    def test_split_patterns_and_imap_search(self):
        self.assertEqual(split_patterns("", ["quantgt"]), [])
        self.assertEqual(split_patterns(None, ["quantgt"]), ["quantgt"])
        self.assertEqual(split_patterns("QuantGT; Picks\nHoldings", []), ["quantgt", "picks", "holdings"])
        # The IMAP search defaults to the official sender with no unseen filter.
        self.assertEqual(search_query({}), 'FROM "quantgt.io"')
        self.assertEqual(search_query({"OFFICIAL_MAIL_IMAP_SEARCH": "UNSEEN"}), "UNSEEN")

    def test_imap_and_gmail_messages_are_decoded_to_official_mails(self):
        mail = official_mail_from_message("1", official_message().as_bytes())
        self.assertEqual((mail.uid, mail.subject), ("1", "Monthly Picks Updated"))
        self.assertIn("support@quantgt.io", mail.from_header)
        self.assertEqual(mail.text.strip(), "New monthly picks are available.")

        mails = gmail_messages_to_official_mails([gmail_message(with_html=True)])
        self.assertEqual(len(mails), 1)
        self.assertEqual((mails[0].uid, mails[0].subject), ("abc123", "Monthly Picks Updated"))
        self.assertIn("support@quantgt.io", mails[0].from_header)
        self.assertIn("New picks are available.", mails[0].text)
        self.assertIn("<p>New picks are available.</p>", mails[0].html)

    def test_matching_requires_official_sender_and_subject(self):
        official = "Quant GT <support@quantgt.io>"
        cases = {
            "official sender and matching subject": (OfficialMail("1", "Monthly Picks Updated", official, "", "", ""), ["picks", "holdings"], True),
            "other sender": (OfficialMail("1", "Monthly Picks Updated", "Other <sender@example.com>", "", "", ""), ["picks", "holdings"], False),
            "unrelated subject": (OfficialMail("1", "Welcome", official, "", "", ""), ["picks", "holdings"], False),
            # A forwarded mail is matched through the quoted original headers in the body.
            "forwarded mail matches through body context": (
                OfficialMail("1", "Fwd: Monthly Picks", "Me <owner@example.com>", "", "From: Quant GT <support@quantgt.io>\nSubject: Monthly Picks Updated", ""),
                ["picks"],
                True,
            ),
            # A plain mention of quantgt.io is not an official sender.
            "plain quantgt mention is not the official sender": (
                OfficialMail("1", "Quant GT Picks Updated", "Me <owner@example.com>", "", "Source: https://quantgt.io\nWeekly picks changed",
                             "<p>Source: https://quantgt.io</p><p>Weekly picks changed</p>"),
                ["picks", "updated"],
                False,
            ),
        }
        for name, (mail, subject_patterns, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(matches_official_mail(mail, ["@quantgt.io"], subject_patterns), expected)

    def test_forward_body_preserves_official_message_context(self):
        mail = OfficialMail("1", "Monthly Picks Updated", "Quant GT <support@quantgt.io>", "Mon, 25 May 2026 08:00:00 +0000",
                            "The picks changed.", "<p>The picks changed.</p>")

        subject, body, html = build_forward_body(mail)

        self.assertEqual(subject, "Quant GT Official Email: Monthly Picks Updated")
        self.assertIn("support@quantgt.io", body)
        self.assertIn("Monthly Picks Updated", body)
        for fragment in ("Forwarded official Quant GT email.", "Quant GT Monitor", "Official Email", "<p>The picks changed.</p>"):
            self.assertIn(fragment, html)


class ForwarderTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_file = self.tmp / "official_mail_forwarder_state.json"
        self.lock_file = self.tmp / "forwarder.lock"
        for name, path in (("STATE_FILE", self.state_file), ("LOG_FILE", self.tmp / "forwarder.log"), ("LOCK_FILE", self.lock_file)):
            patcher = patch(f"{MODULE}.{name}", path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def env(self, **overrides):
        env = {
            "OFFICIAL_MAIL_ENABLED": "1",
            "OFFICIAL_MAIL_PROVIDER": "imap",
            "NOTIFY_EMAIL_TO": "friend@example.com",
            "NOTIFY_EMAIL_FILE": "",
            "NOTIFY_ADMIN_EMAIL_TO": "admin@example.com",
            "NOTIFY_ADMIN_EMAIL_FILE": "",
            "OFFICIAL_MAIL_IMAP_HOST": "imap.example.com",
            "OFFICIAL_MAIL_IMAP_USERNAME": "receiver@example.com",
            "OFFICIAL_MAIL_IMAP_PASSWORD": "secret",
        }
        env.update(overrides)
        return env

    def run_main(self, env, **patches):
        """Run main() the way the daemon does (env from os.environ, stdout captured); returns the JSON it prints."""
        out = io.StringIO()
        with patch.dict("os.environ", env, clear=True), patch(f"{MODULE}.load_env"), patch("sys.argv", ["quantcheck-official-mail"]), redirect_stdout(out):
            main()
        return json.loads(out.getvalue())


class ForwardOfficialMailTests(ForwarderTestCase):
    def test_disabled_by_default(self):
        env = self.env()
        del env["OFFICIAL_MAIL_ENABLED"]
        with patch(f"{MODULE}.connect_imap") as connect_imap:
            result = forward_official_mail(env)
        self.assertEqual(result["skipped"], "disabled")
        connect_imap.assert_not_called()

    def test_gmail_provider_forwards_once_and_marks_message_read(self):
        env = self.env(OFFICIAL_MAIL_PROVIDER="gmail", OFFICIAL_MAIL_GMAIL_QUERY="is:unread")
        with patch(f"{MODULE}.list_gmail_messages", return_value=[gmail_message()]), \
             patch(f"{MODULE}.mark_gmail_message_read") as mark_read, \
             patch(f"{MODULE}.deliver_email", return_value=(["ok@example.com"], [])) as deliver:
            first = forward_official_mail(env)
            second = forward_official_mail(env)

        self.assertEqual((first["provider"], first["forwarded"], second["forwarded"]), ("gmail", 1, 0))
        deliver.assert_called_once()
        mark_read.assert_called_once_with(env, "abc123")

    def test_imap_provider_forwards_once_to_subscribers_and_admins_and_records_state(self):
        env = self.env(OFFICIAL_MAIL_IMAP_SECURITY="starttls")
        with patch(f"{MODULE}.connect_imap", return_value=FakeImap(official_message())), \
             patch(f"{MODULE}.deliver_email", return_value=(["ok@example.com"], [])) as deliver:
            first = forward_official_mail(env)
            second = forward_official_mail(env)

        self.assertEqual((first["forwarded"], second["forwarded"]), (1, 0))
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.kwargs["to"], ["friend@example.com", "admin@example.com"])
        self.assertEqual(len(json.loads(self.state_file.read_text(encoding="utf-8"))["forwarded"]), 1)

    def test_partial_recipient_failure_is_not_marked_forwarded_so_it_is_retried(self):
        with patch(f"{MODULE}.connect_imap", return_value=FakeImap(official_message())), \
             patch(f"{MODULE}.deliver_email", return_value=(["friend@example.com"], ["admin@example.com"])) as deliver:
            first = forward_official_mail(self.env())
            second = forward_official_mail(self.env())

        self.assertEqual((first["forwarded"], first["failed"], second["forwarded"]), (0, 1, 0))
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(deliver.call_args.kwargs["to"], ["friend@example.com", "admin@example.com"])

    def test_forward_state_is_persisted_per_message_not_only_at_end(self):
        # A run that sends one mail and then dies before its own final save (crash, or another run
        # interrupting it) must not lose the dedupe record for the mail it already sent for real.
        imap = FakeImap(official_message(text="First update."), official_message("Weekly picks changed", "Tue, 26 May 2026 08:00:00 +0000", "Second update."))
        with patch(f"{MODULE}.connect_imap", return_value=imap), \
             patch(f"{MODULE}.deliver_email", side_effect=[(["ok@example.com"], []), RuntimeError("simulated crash mid-run")]):
            with self.assertRaises(RuntimeError):
                forward_official_mail(self.env())

        self.assertTrue(self.state_file.exists())
        self.assertEqual(len(json.loads(self.state_file.read_text(encoding="utf-8")).get("forwarded") or []), 1)


class MainTests(ForwarderTestCase):
    def test_failure_alerts_go_only_to_admins(self):
        # Send failure: the subscriber send fails entirely, then an alert goes to admins only.
        sends = [([], ["friend@example.com", "admin@example.com"]), (["admin@example.com"], [])]
        with patch(f"{MODULE}.connect_imap", return_value=FakeImap(official_message())), \
             patch(f"{MODULE}.deliver_email", side_effect=sends) as deliver:
            self.run_main(self.env(OFFICIAL_MAIL_IMAP_SECURITY="starttls"))

        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(deliver.call_args_list[0].kwargs["to"], ["friend@example.com", "admin@example.com"])
        alert = deliver.call_args_list[1].kwargs
        self.assertEqual(alert["to"], ["admin@example.com"])
        self.assertIn("Quant GT Monitor", alert["html"])
        self.assertIn("Official Mail Forward Failed", alert["html"])

        # Check failure (mailbox unreachable): raises, and the alert again goes to admins only.
        with patch(f"{MODULE}.connect_imap", side_effect=RuntimeError("imap down")), \
             patch(f"{MODULE}.deliver_email", return_value=(["ok@example.com"], [])) as deliver:
            with self.assertRaises(RuntimeError):
                self.run_main(self.env(OFFICIAL_MAIL_IMAP_SECURITY="starttls"))

        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.kwargs["to"], ["admin@example.com"])
        self.assertIn("Quant GT Monitor", deliver.call_args.kwargs["html"])
        self.assertIn("Official Mail Check Failed", deliver.call_args.kwargs["html"])

    def test_skips_when_lock_is_already_held(self):
        held_handle = self.lock_file.open("w")
        self.addCleanup(held_handle.close)
        fcntl.flock(held_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with patch(f"{MODULE}.connect_imap") as connect_imap:
            result = self.run_main(self.env())

        self.assertEqual(result["skipped"], "locked")
        connect_imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
