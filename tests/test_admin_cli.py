"""quantcheck-admin is a thin dispatch layer: these tests cover wiring only.

Business rules (expiry maths, bulk semantics, payments, migration) are tested
once in test_service_members.py / test_service_ops.py; here the service calls
are stubbed wherever the point is "which service call, with which params".
"""

import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck import admin_cli
from quantcheck.admin_cli import main
from quantcheck.membership_store import Member, MembershipStore, save_store
from quantcheck.service import ServiceError

UTC = timezone.utc
STUB = {"stub": True}
_PARSER = admin_cli.build_parser()  # building it costs ~50ms (argparse + gettext); share one across all main() calls


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
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(admin_cli, "build_parser", return_value=_PARSER):
            rc = main(["--root", str(self.root), *args])
        return rc, out.getvalue()

    def run_cli_json(self, *args: str):
        rc, out = self.run_cli(*args)
        return rc, json.loads(out)

    def write_store(self, members):
        save_store(MembershipStore(path=self.root / "state" / "memberships.json", members=list(members)), backup=False)

    def store_bytes(self):
        path = self.root / "state" / "memberships.json"
        return path.read_bytes() if path.exists() else None


class CommandWiringTests(AdminCliTestCase):
    """Every command is reachable, maps its flags to the right service call, and emits valid JSON."""

    def test_every_command_maps_args_to_the_service_call(self):
        m, o = admin_cli.members_svc, admin_cli.ops_svc
        # (argv, module, function, expected positional args, expected kwargs minus root)
        cases = [
            (["members", "list", "--status", "expired", "--expiring-days", "7"], m, "list_members", (), {"status": "expired", "expiring_within_days": 7}),
            (["members", "get", "a@x.io"], m, "get_member", ("a@x.io",), {}),
            (
                ["members", "add", "a@x.io", "--months", "2", "--note", "n", "--joined-at", "2026-08-10",
                 "--amount", "99.5", "--currency", "CNY", "--channel", "wechat", "--paid-at", "2026-10-01", "--ref", "T1"],
                m, "add_member", ("a@x.io", 2),
                {"note": "n", "joined_at": "2026-08-10", "expires_at": None, "align": False,
                 "payment": {"amount": "99.5", "currency": "CNY", "channel": "wechat", "paid_at": "2026-10-01", "ref": "T1"}},
            ),
            (["members", "add", "a@x.io", "--expires-at", "2026-11-01"], m, "add_member", ("a@x.io", None),
             {"expires_at": "2026-11-01", "align": False, "payment": None}),
            (["members", "add", "a@x.io", "--align"], m, "add_member", ("a@x.io", None), {"align": True, "expires_at": None}),
            (["members", "extend", "a@x.io", "--months", "2", "--note", "n", "--amount", "10", "--currency", "usd"], m, "extend_member",
             ("a@x.io", 2), {"note": "n", "payment": {"amount": "10", "currency": "usd"}}),
            (["members", "set-expiry", "a@x.io", "--date", "2026-12-09", "--note", "fix"], m, "set_expiry", ("a@x.io", "2026-12-09", "fix"), {}),
            (["members", "set-expiry", "a@x.io", "--date", "null"], m, "set_expiry", ("a@x.io", "null", admin_cli.DEFAULT_SET_EXPIRY_NOTE), {}),
            (["members", "remove", "a@x.io", "--reason", "refund"], m, "remove_member", ("a@x.io", "refund"), {}),
            # bulk commands: positional emails and --file are joined into one blob; dry-run unless --apply
            (["members", "bulk-add", "a@x.io", "b@x.io,bad", "--expires-at", "2026-11-01", "--amount", "5"], m, "bulk_add",
             ("a@x.io\nb@x.io,bad",), {"months": None, "expires_at": "2026-11-01", "align": False, "dry_run": True, "payment": {"amount": "5"}}),
            (["members", "bulk-add", "a@x.io", "--months", "1", "--apply"], m, "bulk_add", ("a@x.io",), {"months": 1, "dry_run": False}),
            (["members", "bulk-extend", "--all-active", "--months", "1", "--note", "renewal"], m, "bulk_extend",
             (None,), {"months": 1, "all_active": True, "note": "renewal", "dry_run": True}),
            (["members", "bulk-extend", "a@x.io", "--months", "1", "--apply"], m, "bulk_extend", ("a@x.io",), {"all_active": False, "dry_run": False}),
            (["members", "bulk-set-expiry", "a@x.io", "--date", "2099-06-09", "--apply"], m, "bulk_set_expiry",
             ("a@x.io",), {"expires_at": "2099-06-09", "all_active": False, "dry_run": False}),
            (["members", "bulk-set-expiry", "--all-active", "--expires-at", "2099-06-09"], m, "bulk_set_expiry",
             (None,), {"expires_at": "2099-06-09", "all_active": True, "dry_run": True}),
            (["members", "record-payment", "a@x.io", "--amount", "100", "--currency", "CNY", "--channel", "alipay",
              "--paid-at", "2026-10-01", "--ref", "r9", "--note", "oct"], m, "record_payment", ("a@x.io", "100"),
             {"currency": "CNY", "channel": "alipay", "paid_at": "2026-10-01", "ref": "r9", "note": "oct"}),
            (["members", "payments", "--since", "2026-09-01", "--until", "2026-10-01"], m, "payments_report", (), {"since": "2026-09-01", "until": "2026-10-01"}),
            (["members", "migrate", "--expires", "2026-10-09", "--note", "n", "--dry-run"], m, "migrate_from_recipients", ("2026-10-09", "n"), {"dry_run": True}),
            (["members", "migrate", "--expires", "2026-10-09", "--note", "n"], m, "migrate_from_recipients", ("2026-10-09", "n"), {"dry_run": False}),
            (["members", "sync"], m, "sync_recipients", (), {}),
            (["ops", "status"], o, "status", (), {}),
            (["ops", "diagnose"], o, "diagnose", (), {}),
            (["ops", "run", "picks", "--force", "--confirm", "--timeout", "30"], o, "run_job", ("picks",), {"force": True, "confirm": True, "timeout": 30}),
            (["ops", "run", "test_email"], o, "run_job", ("test_email",), {"force": False, "confirm": False, "timeout": None}),
            (["ops", "logs", "scheduler", "--lines", "5", "--grep", "x"], o, "logs", ("scheduler",), {"lines": 5, "grep": "x"}),
            (["ops", "deliveries", "--limit", "7"], o, "recent_deliveries", (), {"limit": 7}),
            (["ops", "schedule-preview", "--days", "2"], o, "schedule_preview", (), {"days": 2}),
            (["ops", "resend-preview", "--weekly-date", "Updated on Jan 1, 2026"], o, "historical_resend_preview", ("Updated on Jan 1, 2026",), {}),
        ]
        for argv, module, name, args, kwargs in cases:
            with self.subTest(argv=" ".join(argv)):
                with patch.object(module, name, return_value=STUB) as service:
                    rc, data = self.run_cli_json(*argv)
                self.assertEqual((rc, data), (0, STUB))
                service.assert_called_once()
                call = service.call_args
                self.assertEqual(call.args, args)
                self.assertEqual(call.kwargs.pop("root"), self.root)
                self.assertEqual({k: call.kwargs[k] for k in kwargs}, kwargs)

    def test_migrate_default_note_and_route_preview_wiring(self):
        with patch.object(admin_cli.members_svc, "migrate_from_recipients", return_value=STUB) as migrate:
            self.run_cli_json("members", "migrate", "--expires", "2026-10-09")
        self.assertTrue(migrate.call_args.args[1].startswith("migrated from notify_recipients.txt on "))

        for argv, route in ((["route", "preview"], "picks_update"), (["route", "preview", "--route", "admin"], "admin")):
            with self.subTest(route=route), patch.object(admin_cli, "route_preview", return_value=STUB) as preview:
                rc, data = self.run_cli_json(*argv)
                self.assertEqual((rc, data), (0, STUB))
                self.assertEqual(preview.call_args.args[0], route)
                self.assertEqual(preview.call_args.kwargs["root"], self.root)

    def test_real_commands_emit_valid_json_against_a_tmp_root(self):
        (self.root / "notify_recipients.txt").write_text("a@example.com\n", encoding="utf-8")
        (self.root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        self.assertEqual(self.run_cli_json("members", "list"), (0, {"count": 0, "members": []}))
        rc, data = self.run_cli_json("members", "add", "new@example.com", "--months", "1", "--joined-at", "2026-08-10")
        self.assertEqual(rc, 0)
        self.assertEqual(set(data), {"member", "recipients_file", "store_backup"})
        self.assertEqual(data["member"]["expires_at"], "2026-09-09T00:00:00-04:00")
        self.assertEqual(self.run_cli_json("members", "get", "new@example.com")[1]["member"]["email"], "new@example.com")
        rc, data = self.run_cli_json("members", "sync")
        self.assertEqual((rc, "in_sync" in data), (0, True))
        for argv, key in ((["ops", "diagnose"], "findings"), (["ops", "deliveries"], "deliveries")):
            with self.subTest(argv=argv):
                rc, data = self.run_cli_json(*argv)
                self.assertEqual(rc, 0)
                self.assertIn(key, data)
        rc, data = self.run_cli_json("ops", "resend-preview", "--weekly-date", "Updated on Jan 1, 2026")
        self.assertEqual((rc, data["error"]["code"]), (2, "resend_validation_failed"))

    def test_route_preview_reads_the_given_root_never_the_production_one(self):
        # Regression for the ROOT-freezing bug: --root <tmp> must never read the real subscriber list.
        (self.root / "notify_recipients.txt").write_text("only-this-one@example.com\n", encoding="utf-8")
        (self.root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        rc, data = self.run_cli_json("route", "preview")
        self.assertEqual(rc, 0)
        self.assertEqual(data["route"], "picks_update")
        self.assertEqual(data["counts"]["subscribers_total"], 1)
        self.assertEqual(set(data["included"]), {"only-this-one@example.com", "admin@example.com"})
        self.assertNotIn("zhoucehuang@gmail.com", data["included"])  # a real production admin address

        rc, data = self.run_cli_json("route", "preview", "--route", "admin")
        self.assertEqual(data["included"], ["admin@example.com"])


class DryRunAndConfirmFlagTests(AdminCliTestCase):
    def test_bulk_commands_dry_run_by_default_and_apply_writes(self):
        recipients = self.root / "notify_recipients.txt"
        recipients.write_text("", encoding="utf-8")
        self.run_cli("members", "add", "a@example.com", "--expires-at", "2099-01-09")
        before_store, before_recipients = self.store_bytes(), recipients.read_bytes()

        dry_runs = [
            ("members", "bulk-add", "n1@example.com", "--expires-at", "2026-11-01"),
            ("members", "bulk-extend", "--all-active", "--months", "1"),
            ("members", "bulk-set-expiry", "--all-active", "--expires-at", "2099-06-09"),
        ]
        for argv in dry_runs:
            with self.subTest(argv=argv):
                rc, data = self.run_cli_json(*argv)
                self.assertEqual((rc, data["dry_run"]), (0, True))
                self.assertEqual((self.store_bytes(), recipients.read_bytes()), (before_store, before_recipients))  # byte-identical

        rc, data = self.run_cli_json(*dry_runs[0], "--apply")
        self.assertEqual((rc, data["dry_run"]), (0, False))
        self.assertNotEqual(self.store_bytes(), before_store)
        self.assertIn("n1@example.com", recipients.read_text(encoding="utf-8"))

    def test_migrate_dry_run_flag_writes_nothing(self):
        (self.root / "notify_recipients.txt").write_text("a@example.com\nb@example.com\n", encoding="utf-8")
        rc, data = self.run_cli_json("members", "migrate", "--expires", "2026-10-09", "--dry-run")
        self.assertEqual((rc, data["dry_run"], data["to_migrate_count"]), (0, True, 2))
        self.assertIsNone(self.store_bytes())

        rc, data = self.run_cli_json("members", "migrate", "--expires", "2026-10-09")
        self.assertEqual((rc, data["migrated_count"]), (0, 2))

    def test_ops_run_requires_confirm_for_test_email_and_forced_picks(self):
        for argv in (("ops", "run", "test_email"), ("ops", "run", "picks", "--force")):
            with self.subTest(argv=argv), patch("quantcheck.service.ops.scheduler_mod.run_cmd") as run_cmd:
                rc, data = self.run_cli_json(*argv)
                self.assertEqual((rc, data["error"]["code"]), (2, "confirmation_required"))
                run_cmd.assert_not_called()

        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")):
            rc, data = self.run_cli_json("ops", "run", "picks")  # unforced picks needs no confirm
        self.assertEqual((rc, data["ok"]), (0, True))

    def test_bulk_inputs_from_file_stdin_and_unreadable_file(self):
        (self.root / "notify_recipients.txt").write_text("", encoding="utf-8")
        path = self.root / "list.txt"
        path.write_text("# new batch\nf1@example.com; f2@example.com  # comment\n", encoding="utf-8")
        rc, data = self.run_cli_json("members", "bulk-add", "--file", str(path), "--expires-at", "2026-11-01")
        self.assertEqual((rc, [r["email"] for r in data["results"]]), (0, ["f1@example.com", "f2@example.com"]))
        with patch("sys.stdin", io.StringIO("s1@example.com\ns2@example.com\n")):
            rc, data = self.run_cli_json("members", "bulk-add", "--file", "-", "--expires-at", "2026-11-01")
        self.assertEqual([r["email"] for r in data["results"]], ["s1@example.com", "s2@example.com"])

        rc, data = self.run_cli_json("members", "bulk-add", "--file", str(self.root / "nope.txt"), "--expires-at", "2026-11-01")
        self.assertEqual((rc, data["error"]["code"]), (2, "file_unreadable"))


class ErrorEnvelopeAndExitCodeTests(AdminCliTestCase):
    def test_service_error_is_a_json_envelope_with_exit_2(self):
        rc, data = self.run_cli_json("members", "get", "nobody@example.com")
        self.assertEqual((rc, data["error"]["code"]), (2, "member_not_found"))
        with patch.object(admin_cli.members_svc, "get_member", side_effect=ServiceError("boom", "msg", {"k": 1})):
            rc, data = self.run_cli_json("members", "get", "a@example.com")
        self.assertEqual((rc, data), (2, {"error": {"code": "boom", "message": "msg", "details": {"k": 1}}}))

    def test_unexpected_exception_is_internal_error_with_exit_3(self):
        with patch.object(admin_cli.members_svc, "list_members", side_effect=RuntimeError("kaput")):
            rc, data = self.run_cli_json("members", "list")
        self.assertEqual(rc, 3)
        self.assertEqual(data, {"error": {"code": "internal_error", "message": "kaput", "details": {"type": "RuntimeError"}}})

    def test_malformed_invocation_is_rejected_by_argparse_before_the_json_envelope(self):
        # argparse raises SystemExit(2) with a usage message on stderr before main()'s try/except
        # runs: a malformed invocation, not a business-logic failure.
        err = io.StringIO()
        with redirect_stderr(err), patch.object(admin_cli, "build_parser", return_value=_PARSER), self.assertRaises(SystemExit) as ctx:
            main(["--root", str(self.root), "ops", "logs", "not-a-real-log"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())


class HumanRenderingTests(AdminCliTestCase):
    def test_human_flag_renders_readable_non_json_output(self):
        rc, out = self.run_cli("--human", "members", "list")  # empty list must not crash
        self.assertEqual(rc, 0)

        self.write_store([Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=UTC), expires_at=datetime(2099, 1, 1, tzinfo=UTC))])
        rc, out = self.run_cli("--human", "members", "list")
        self.assertEqual(rc, 0)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)
        self.assertIn("a@example.com", out)

        (self.root / "notify_recipients.txt").write_text("", encoding="utf-8")
        rc, out = self.run_cli("--human", "members", "bulk-add", "b@example.com", "--expires-at", "2026-11-01")
        self.assertEqual(rc, 0)
        self.assertIn("results (1):", out)
        self.assertIn("email=b@example.com", out)

    def test_errors_are_rendered_in_human_mode_too(self):
        rc, out = self.run_cli("--human", "members", "get", "nobody@example.com")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)
        self.assertIn("member_not_found", out)


class EntryPointTests(unittest.TestCase):
    def test_all_expected_console_scripts_are_declared_in_pyproject(self):
        import tomllib

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        names = set(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"])
        expected = {
            "quantcheck",
            "quantcheck-picks",
            "quantcheck-report",
            "quantcheck-health",
            "quantcheck-official-mail",
            "quantcheck-recipients",
            "quantcheck-admin",
            "quantcheck-mcp",
        }
        self.assertTrue(expected.issubset(names), f"missing: {expected - names}")


if __name__ == "__main__":
    unittest.main()
