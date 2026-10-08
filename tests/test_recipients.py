import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quantcheck.recipients import main


class RecipientCliTests(unittest.TestCase):
    def run_cli(self, root: Path, *args: str):
        buf = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(buf):
            rc = main(["--root", str(root), *args])
        return rc, buf.getvalue()

    def write_env(self, root: Path) -> None:
        (root / ".env").write_text(
            "QUANTCHECK_HOME={root}\n"
            "NOTIFY_EMAIL_FILE=notify_recipients.txt\n"
            "NOTIFY_ADMIN_EMAIL_FILE=notify_admin_recipients.txt\n"
            "NOTIFY_EMAIL_TO=inline@example.com\n"
            "NOTIFY_ADMIN_EMAIL_TO=\n".format(root=root),
            encoding="utf-8",
        )

    def test_add_normalizes_dedupes_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)
            recipient_file = root / "notify_recipients.txt"
            recipient_file.write_text("# recipients\nfriend@example.com\n", encoding="utf-8")

            rc, out = self.run_cli(root, "add", "FRIEND@example.com", "new@example.com")

            self.assertEqual(rc, 0)
            self.assertIn("added 1 subscribers recipient(s): new@example.com", out)
            self.assertEqual(
                recipient_file.read_text(encoding="utf-8").splitlines()[-2:],
                ["friend@example.com", "new@example.com"],
            )
            self.assertEqual(len(list(root.glob("notify_recipients.txt.*.bak"))), 1)

    def test_add_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)
            recipient_file = root / "notify_recipients.txt"
            recipient_file.write_text("friend@example.com\n", encoding="utf-8")

            rc, out = self.run_cli(root, "add", "--dry-run", "new@example.com")

            self.assertEqual(rc, 0)
            self.assertIn("would add 1 subscribers recipient(s): new@example.com", out)
            self.assertEqual(recipient_file.read_text(encoding="utf-8"), "friend@example.com\n")
            self.assertEqual(list(root.glob("*.bak")), [])

    def test_remove_recipient(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)
            recipient_file = root / "notify_recipients.txt"
            recipient_file.write_text("friend@example.com\nother@example.com\n", encoding="utf-8")

            rc, out = self.run_cli(root, "remove", "friend@example.com", "missing@example.com")

            self.assertEqual(rc, 0)
            self.assertIn("removed 1 subscribers recipient(s): friend@example.com", out)
            self.assertIn("not present: missing@example.com", out)
            self.assertNotIn("friend@example.com", recipient_file.read_text(encoding="utf-8"))
            self.assertIn("other@example.com", recipient_file.read_text(encoding="utf-8"))

    def test_admin_role_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)
            (root / "notify_recipients.txt").write_text("friend@example.com\n", encoding="utf-8")
            admin_file = root / "notify_admin_recipients.txt"
            admin_file.write_text("admin@example.com\n", encoding="utf-8")

            rc, out = self.run_cli(root, "add", "--role", "admin", "ops@example.com")

            self.assertEqual(rc, 0)
            self.assertIn("added 1 admins recipient(s): ops@example.com", out)
            self.assertIn("ops@example.com", admin_file.read_text(encoding="utf-8"))
            self.assertNotIn("ops@example.com", (root / "notify_recipients.txt").read_text(encoding="utf-8"))

    def test_invalid_email_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)

            with patch("sys.stderr", new_callable=io.StringIO) as err:
                rc, _ = self.run_cli(root, "add", "bad-address")

            self.assertEqual(rc, 2)
            self.assertIn("invalid email address", err.getvalue())

    def test_check_reports_invalid_file_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_env(root)
            (root / "notify_recipients.txt").write_text("friend@example.com\nbad-address\n", encoding="utf-8")
            (root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")

            rc, out = self.run_cli(root, "check")

            self.assertEqual(rc, 1)
            self.assertIn("invalid entries: bad-address", out)
            self.assertIn("picks-update route total: 3", out)
            self.assertIn("admin route total: 1", out)


if __name__ == "__main__":
    unittest.main()
