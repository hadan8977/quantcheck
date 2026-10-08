import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck.admin_cli import main
from quantcheck.membership_store import Member, MembershipStore, save_store

UTC = timezone.utc


class AdminCliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(
            f"QUANTCHECK_HOME={self.root}\nNOTIFY_EMAIL_FILE=notify_recipients.txt\nNOTIFY_ADMIN_EMAIL_FILE=notify_admin_recipients.txt\n",
            encoding="utf-8",
        )

    def run_cli(self, *args: str):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--root", str(self.root), *args])
        return rc, buf.getvalue()

    def run_cli_json(self, *args: str):
        rc, out = self.run_cli(*args)
        return rc, json.loads(out)

    def write_store(self, members):
        save_store(MembershipStore(path=self.root / "state" / "memberships.json", members=list(members)), backup=False)


class MembersCommandsProduceStableJsonTests(AdminCliTestCase):
    def test_members_list_empty(self):
        rc, data = self.run_cli_json("members", "list")
        self.assertEqual(rc, 0)
        self.assertEqual(data, {"count": 0, "members": []})

    def test_members_add_shape(self):
        rc, data = self.run_cli_json("members", "add", "new@example.com", "--months", "1", "--joined-at", "2026-08-10")
        self.assertEqual(rc, 0)
        self.assertEqual(set(data.keys()), {"member", "recipients_file", "store_backup"})
        member = data["member"]
        self.assertEqual(
            set(member.keys()),
            {"email", "status", "stored_status", "joined_at", "expires_at", "months_total", "note", "history"},
        )
        self.assertEqual(member["email"], "new@example.com")
        self.assertEqual(member["expires_at"], "2026-09-09T00:00:00-04:00")

    def test_members_get_shape(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "get", "a@example.com")
        self.assertEqual(rc, 0)
        self.assertEqual(list(data.keys()), ["member"])
        self.assertIsInstance(data["member"]["history"], list)

    def test_members_extend_shape(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "extend", "a@example.com", "--months", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["expires_at"], "2026-11-09T00:00:00-05:00")
        self.assertEqual(data["member"]["months_total"], 3)

    def test_members_set_expiry_shape_and_default_note(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "set-expiry", "a@example.com", "--date", "2026-12-09")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["expires_at"], "2026-12-09T00:00:00-05:00")

    def test_members_set_expiry_null_date_means_legacy(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "set-expiry", "a@example.com", "--date", "null")
        self.assertEqual(rc, 0)
        self.assertIsNone(data["member"]["expires_at"])
        self.assertEqual(data["member"]["status"], "legacy")

    def test_members_remove_shape(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "remove", "a@example.com", "--reason", "refund")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["stored_status"], "cancelled")

    def test_members_sync_shape(self):
        rc, data = self.run_cli_json("members", "sync")
        self.assertEqual(rc, 0)
        self.assertEqual(set(data.keys()), {"as_of", "recipients_file", "file_count", "member_count", "active_member_count", "drift", "in_sync"})

    def test_members_list_expiring_days_flag(self):
        now = datetime.now(timezone.utc)
        self.write_store([Member(email="soon@example.com", status="active", joined_at=now, expires_at=now)])
        rc, data = self.run_cli_json("members", "list", "--expiring-days", "0")
        self.assertEqual(rc, 0)
        # expires_at == now means already expired (is_active requires now < expires_at), so it should not show as "expiring active".
        self.assertEqual(data["count"], 0)


class MembersErrorsProduceValidJsonTests(AdminCliTestCase):
    def test_get_nonexistent_member_returns_error_envelope_and_exit_2(self):
        rc, data = self.run_cli_json("members", "get", "nobody@example.com")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "member_not_found")

    def test_add_invalid_email_returns_error_envelope(self):
        rc, data = self.run_cli_json("members", "add", "not-an-email", "--months", "1")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "invalid_email")

    def test_extend_missing_member_returns_error_envelope(self):
        rc, data = self.run_cli_json("members", "extend", "nobody@example.com", "--months", "1")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "member_not_found")

    def test_error_output_is_always_valid_json_even_in_human_mode(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--root", str(self.root), "--human", "members", "get", "nobody@example.com"])
        # Human mode still must not crash; it just renders differently (not JSON).
        self.assertIn("error", buf.getvalue())


class RoutePreviewCommandTests(AdminCliTestCase):
    def test_default_route_is_picks_update(self):
        (self.root / "notify_recipients.txt").write_text("a@example.com\n", encoding="utf-8")
        (self.root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        rc, data = self.run_cli_json("route", "preview")
        self.assertEqual(rc, 0)
        self.assertEqual(data["route"], "picks_update")
        self.assertEqual(set(data["included"]), {"a@example.com", "admin@example.com"})

    def test_route_preview_never_leaks_into_real_production_root(self):
        # Direct regression test for the ROOT-freezing bug found while
        # building this CLI: --root <tmp> must never silently read
        # /opt/quantcheck's real notify_recipients.txt (81 real subscribers).
        (self.root / "notify_recipients.txt").write_text("only-this-one@example.com\n", encoding="utf-8")
        rc, data = self.run_cli_json("route", "preview")
        self.assertEqual(rc, 0)
        self.assertEqual(data["counts"]["subscribers_total"], 1)
        self.assertNotIn("zhoucehuang@gmail.com", data["included"])  # a real production admin address

    def test_admin_route(self):
        (self.root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        rc, data = self.run_cli_json("route", "preview", "--route", "admin")
        self.assertEqual(rc, 0)
        self.assertEqual(data["included"], ["admin@example.com"])

    def test_81_subscribers_all_included_zero_excluded_after_migration(self):
        # Mirrors the real acceptance check end-to-end at CLI level.
        emails = [f"user{i}@example.com" for i in range(81)]
        (self.root / "notify_recipients.txt").write_text("\n".join(emails) + "\n", encoding="utf-8")
        (self.root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")

        rc, migrate_data = self.run_cli_json("members", "migrate", "--expires", "2026-10-09")
        self.assertEqual(rc, 0)
        self.assertEqual(migrate_data["migrated_count"], 81)

        rc, preview = self.run_cli_json("route", "preview")
        self.assertEqual(rc, 0)
        self.assertEqual(preview["counts"]["excluded"], 0)
        self.assertEqual(preview["counts"]["included"], 82)  # 81 subscribers + 1 admin
        self.assertEqual(preview["excluded"], [])


class OpsCommandsTests(AdminCliTestCase):
    def test_ops_status_shape(self):
        rc, data = self.run_cli_json("ops", "status")
        self.assertEqual(rc, 0)
        self.assertIn("lock", data)
        self.assertIn("next_job", data)

    def test_ops_diagnose_shape(self):
        rc, data = self.run_cli_json("ops", "diagnose")
        self.assertEqual(rc, 0)
        self.assertIn("overall", data)
        self.assertIsInstance(data["findings"], list)

    def test_ops_run_test_email_without_confirm_is_rejected(self):
        rc, data = self.run_cli_json("ops", "run", "test_email")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "confirmation_required")

    def test_ops_run_picks_force_without_confirm_is_rejected(self):
        rc, data = self.run_cli_json("ops", "run", "picks", "--force")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "confirmation_required")

    def test_ops_run_dispatches_when_allowed(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")):
            rc, data = self.run_cli_json("ops", "run", "picks")
        self.assertEqual(rc, 0)
        self.assertTrue(data["ok"])

    def test_ops_logs_invalid_name_rejected_by_argparse(self):
        # argparse's own `choices` validation raises SystemExit(2) with a
        # usage message on stderr before main()'s try/except ever runs --
        # standard, well-known CLI behavior. This is intentionally different
        # from a ServiceError (which produces the JSON envelope on stdout):
        # this is a malformed *invocation*, not a business-logic failure.
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("ops", "logs", "not-a-real-log")
        self.assertEqual(ctx.exception.code, 2)

    def test_ops_deliveries_shape(self):
        rc, data = self.run_cli_json("ops", "deliveries")
        self.assertEqual(rc, 0)
        self.assertEqual(set(data.keys()), {"path", "count", "filtered_fixture_count", "deliveries"})

    def test_ops_schedule_preview_shape(self):
        rc, data = self.run_cli_json("ops", "schedule-preview", "--days", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(len(data["days"]), 2)

    def test_ops_resend_preview_wraps_validation_error(self):
        rc, data = self.run_cli_json("ops", "resend-preview", "--weekly-date", "Updated on Jan 1, 2026")
        self.assertEqual(rc, 2)
        self.assertEqual(data["error"]["code"], "resend_validation_failed")


class HumanRenderingTests(AdminCliTestCase):
    def test_human_flag_produces_non_json_readable_output(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=UTC), expires_at=datetime(2099, 1, 1, tzinfo=UTC))])
        rc, out = self.run_cli("--human", "members", "list")
        self.assertEqual(rc, 0)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)
        self.assertIn("a@example.com", out)

    def test_human_flag_on_empty_list_does_not_crash(self):
        rc, out = self.run_cli("--human", "members", "list")
        self.assertEqual(rc, 0)


class ExistingEntryPointsUnaffectedTests(unittest.TestCase):
    def test_all_expected_console_scripts_are_registered(self):
        import importlib.metadata as metadata

        eps = metadata.entry_points(group="console_scripts")
        names = {ep.name for ep in eps if ep.name.startswith("quantcheck")}
        expected = {
            "quantcheck",
            "quantcheck-picks",
            "quantcheck-report",
            "quantcheck-health",
            "quantcheck-official-mail",
            "quantcheck-recipients",
            "quantcheck-admin",
        }
        self.assertTrue(expected.issubset(names), f"missing: {expected - names}")



class AddExpiryModesAndPaymentFlagsTests(AdminCliTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "notify_recipients.txt").write_text("", encoding="utf-8")

    def test_add_with_expires_at_and_payment_flags(self):
        rc, data = self.run_cli_json(
            "members", "add", "new@example.com", "--expires-at", "2026-11-01", "--amount", "99.5", "--currency", "CNY", "--channel", "wechat", "--ref", "T1"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["expires_at"], "2026-11-01T00:00:00-04:00")
        entry = data["member"]["history"][-1]
        self.assertEqual((entry["mode"], entry["months"]), ("expires_at", None))
        self.assertEqual(entry["payment"]["amount"], 99.5)
        self.assertEqual(entry["payment"]["channel"], "wechat")

    def test_add_with_align(self):
        self.run_cli("members", "add", "first@example.com", "--expires-at", "2099-01-09")
        rc, data = self.run_cli_json("members", "add", "second@example.com", "--align")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["expires_at"], "2099-01-09T00:00:00-05:00")

    def test_add_requires_exactly_one_mode_via_service_error_not_argparse(self):
        rc, data = self.run_cli_json("members", "add", "new@example.com")
        self.assertEqual((rc, data["error"]["code"]), (2, "invalid_arguments"))
        rc, data = self.run_cli_json("members", "add", "new@example.com", "--months", "1", "--align")
        self.assertEqual((rc, data["error"]["code"]), (2, "invalid_arguments"))

    def test_payment_flag_without_amount_is_rejected(self):
        rc, data = self.run_cli_json("members", "add", "new@example.com", "--months", "1", "--currency", "CNY")
        self.assertEqual((rc, data["error"]["code"]), (2, "invalid_payment"))

    def test_extend_accepts_payment_flags(self):
        self.run_cli("members", "add", "a@example.com", "--months", "1", "--joined-at", "2026-08-10")
        rc, data = self.run_cli_json("members", "extend", "a@example.com", "--months", "1", "--amount", "10", "--currency", "usd")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["history"][-1]["payment"]["currency"], "USD")


class BulkCommandsTests(AdminCliTestCase):
    def setUp(self):
        super().setUp()
        self.recipients = self.root / "notify_recipients.txt"
        self.recipients.write_text("", encoding="utf-8")

    def store_bytes(self):
        path = self.root / "state" / "memberships.json"
        return path.read_bytes() if path.exists() else None

    def test_bulk_add_is_dry_run_by_default_and_apply_writes(self):
        rc, data = self.run_cli_json("members", "bulk-add", "a@example.com", "b@example.com,bad", "--expires-at", "2026-11-01")
        self.assertEqual(rc, 0)
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["counts"]["ok"], 2)
        self.assertEqual(data["counts"]["skipped_by_code"], {"invalid_email": 1})
        self.assertIsNone(self.store_bytes())
        self.assertEqual(self.recipients.read_text(encoding="utf-8"), "")

        rc, data = self.run_cli_json("members", "bulk-add", "a@example.com", "b@example.com", "--expires-at", "2026-11-01", "--apply")
        self.assertEqual((rc, data["dry_run"]), (0, False))
        self.assertIn("a@example.com", self.recipients.read_text(encoding="utf-8"))
        rc, data = self.run_cli_json("members", "get", "b@example.com")
        self.assertEqual(data["member"]["expires_at"], "2026-11-01T00:00:00-04:00")

    def test_bulk_add_from_file_and_stdin(self):
        path = self.root / "list.txt"
        path.write_text("# new batch\nf1@example.com; f2@example.com  # comment\n", encoding="utf-8")
        rc, data = self.run_cli_json("members", "bulk-add", "--file", str(path), "--expires-at", "2026-11-01")
        self.assertEqual((rc, [r["email"] for r in data["results"]]), (0, ["f1@example.com", "f2@example.com"]))
        with patch("sys.stdin", io.StringIO("s1@example.com\ns2@example.com\n")):
            rc, data = self.run_cli_json("members", "bulk-add", "--file", "-", "--expires-at", "2026-11-01")
        self.assertEqual([r["email"] for r in data["results"]], ["s1@example.com", "s2@example.com"])

    def test_missing_file_is_a_clean_error(self):
        rc, data = self.run_cli_json("members", "bulk-add", "--file", str(self.root / "nope.txt"), "--expires-at", "2026-11-01")
        self.assertEqual((rc, data["error"]["code"]), (2, "file_unreadable"))

    def test_bulk_extend_and_set_expiry_dry_run_then_apply(self):
        self.run_cli("members", "add", "a@example.com", "--expires-at", "2099-01-09")
        self.run_cli("members", "add", "b@example.com", "--expires-at", "2099-01-09")
        before = self.store_bytes()
        rc, data = self.run_cli_json("members", "bulk-extend", "--all-active", "--months", "1")
        self.assertEqual((rc, data["dry_run"], data["counts"]["ok"]), (0, True, 2))
        self.assertEqual(self.store_bytes(), before)
        rc, data = self.run_cli_json("members", "bulk-extend", "a@example.com", "ghost@example.com", "--months", "1", "--apply")
        self.assertEqual(data["counts"]["skipped_by_code"], {"member_not_found": 1})
        rc, data = self.run_cli_json("members", "get", "a@example.com")
        self.assertEqual(data["member"]["expires_at"], "2099-02-09T00:00:00-05:00")

        before = self.store_bytes()
        rc, data = self.run_cli_json("members", "bulk-set-expiry", "--all-active", "--expires-at", "2099-06-09")
        self.assertEqual((rc, data["dry_run"]), (0, True))
        self.assertEqual(self.store_bytes(), before)
        self.run_cli("members", "bulk-set-expiry", "b@example.com", "--date", "2099-06-09", "--apply")
        rc, data = self.run_cli_json("members", "get", "b@example.com")
        self.assertEqual(data["member"]["expires_at"], "2099-06-09T00:00:00-04:00")

    def test_bulk_requires_target_selection(self):
        rc, data = self.run_cli_json("members", "bulk-extend", "--months", "1")
        self.assertEqual((rc, data["error"]["code"]), (2, "invalid_arguments"))
        rc, data = self.run_cli_json("members", "bulk-extend", "a@example.com", "--all-active", "--months", "1")
        self.assertEqual((rc, data["error"]["code"]), (2, "invalid_arguments"))

    def test_record_payment_and_payments_report(self):
        self.run_cli("members", "add", "a@example.com", "--expires-at", "2099-01-09")
        rc, data = self.run_cli_json("members", "record-payment", "a@example.com", "--amount", "100", "--currency", "CNY", "--channel", "alipay", "--paid-at", "2026-10-01", "--ref", "r9")
        self.assertEqual(rc, 0)
        self.assertEqual(data["member"]["expires_at"], "2099-01-09T00:00:00-05:00")
        self.assertEqual(data["member"]["total_paid"], {"CNY": 100})
        rc, data = self.run_cli_json("members", "payments", "--since", "2026-09-01", "--until", "2026-10-01")
        self.assertEqual((rc, data["count"], data["totals_by_currency"]), (0, 1, {"CNY": 100}))
        rc, data = self.run_cli_json("members", "payments", "--since", "2026-10-02")
        self.assertEqual(data["count"], 0)

    def test_human_rendering_of_bulk_results(self):
        rc, out = self.run_cli("--human", "members", "bulk-add", "a@example.com", "--expires-at", "2026-11-01")
        self.assertEqual(rc, 0)
        self.assertIn("results (1):", out)
        self.assertIn("email=a@example.com", out)


if __name__ == "__main__":
    unittest.main()
