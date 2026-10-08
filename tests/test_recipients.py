import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quantcheck.recipients import main


class RecipientCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".env").write_text(
            f"QUANTCHECK_HOME={self.root}\n"
            "NOTIFY_EMAIL_FILE=notify_recipients.txt\n"
            "NOTIFY_ADMIN_EMAIL_FILE=notify_admin_recipients.txt\n"
            "NOTIFY_EMAIL_TO=inline@example.com\n"
            "NOTIFY_ADMIN_EMAIL_TO=\n",
            encoding="utf-8",
        )
        self.subscribers = self.root / "notify_recipients.txt"
        self.admins = self.root / "notify_admin_recipients.txt"

    def run_cli(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(out), redirect_stderr(err):
            rc = main(["--root", str(self.root), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_add_normalizes_dedupes_and_creates_backup_but_dry_run_writes_nothing(self):
        self.subscribers.write_text("# recipients\nfriend@example.com\n", encoding="utf-8")

        rc, out, _ = self.run_cli("add", "--dry-run", "new@example.com")
        self.assertEqual(rc, 0)
        self.assertIn("would add 1 subscribers recipient(s): new@example.com", out)
        self.assertEqual(self.subscribers.read_text(encoding="utf-8"), "# recipients\nfriend@example.com\n")
        self.assertEqual(list(self.root.glob("*.bak")), [])

        rc, out, _ = self.run_cli("add", "FRIEND@example.com", "new@example.com")
        self.assertEqual(rc, 0)
        self.assertIn("added 1 subscribers recipient(s): new@example.com", out)
        self.assertEqual(self.subscribers.read_text(encoding="utf-8").splitlines()[-2:], ["friend@example.com", "new@example.com"])
        self.assertEqual(len(list(self.root.glob("notify_recipients.txt.*.bak"))), 1)

    def test_remove_recipient(self):
        self.subscribers.write_text("friend@example.com\nother@example.com\n", encoding="utf-8")

        rc, out, _ = self.run_cli("remove", "friend@example.com", "missing@example.com")

        self.assertEqual(rc, 0)
        self.assertIn("removed 1 subscribers recipient(s): friend@example.com", out)
        self.assertIn("not present: missing@example.com", out)
        self.assertNotIn("friend@example.com", self.subscribers.read_text(encoding="utf-8"))
        self.assertIn("other@example.com", self.subscribers.read_text(encoding="utf-8"))

    def test_admin_role_is_separate(self):
        self.subscribers.write_text("friend@example.com\n", encoding="utf-8")
        self.admins.write_text("admin@example.com\n", encoding="utf-8")

        rc, out, _ = self.run_cli("add", "--role", "admin", "ops@example.com")

        self.assertEqual(rc, 0)
        self.assertIn("added 1 admins recipient(s): ops@example.com", out)
        self.assertIn("ops@example.com", self.admins.read_text(encoding="utf-8"))
        self.assertNotIn("ops@example.com", self.subscribers.read_text(encoding="utf-8"))

    def test_invalid_email_returns_error_and_check_reports_invalid_file_entries(self):
        rc, _, err = self.run_cli("add", "bad-address")
        self.assertEqual(rc, 2)
        self.assertIn("invalid email address", err)

        self.subscribers.write_text("friend@example.com\nbad-address\n", encoding="utf-8")
        self.admins.write_text("admin@example.com\n", encoding="utf-8")
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("invalid entries: bad-address", out)
        self.assertIn("picks-update route total: 3", out)
        self.assertIn("admin route total: 1", out)


if __name__ == "__main__":
    unittest.main()
