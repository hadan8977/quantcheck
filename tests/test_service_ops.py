import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import _fast_calendar  # noqa: F401  -- fast trading-day lookup for the whole suite
import fcntl
import json
import tempfile
import unittest
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from quantcheck.service import ServiceError
from quantcheck.service import ops as svc_ops


class ServiceOpsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\n", encoding="utf-8")

    def write_json(self, relative: str, data, root: Path | None = None) -> Path:
        path = (root or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def fresh_root(self) -> Path:
        """An extra empty install root, for table-driven subTests that need clean state each."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "state").mkdir()
        (root / "logs").mkdir()
        (root / ".env").write_text(f"QUANTCHECK_HOME={root}\n", encoding="utf-8")
        return root

    def hold_lock(self):
        lock_path = self.root / "state" / "quantcheck.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle


RUN_CMD = "quantcheck.service.ops.scheduler_mod.run_cmd"


class RunJobTests(ServiceOpsTestCase):
    def test_invalid_kind_and_confirm_gates_reject_before_running_anything(self):
        cases = [
            ("invalid_job_kind", "not_a_kind", {}),
            ("confirmation_required", "test_email", {}),
            ("confirmation_required", "picks", {"force": True}),
        ]
        for code, kind, kwargs in cases:
            with self.subTest(kind=kind, kwargs=kwargs), patch(RUN_CMD) as run_cmd:
                with self.assertRaises(ServiceError) as ctx:
                    svc_ops.run_job(kind, root=self.root, **kwargs)
                self.assertEqual(ctx.exception.code, code)
                run_cmd.assert_not_called()

    def test_dispatch_and_which_kinds_need_no_confirm(self):
        # (kind, kwargs, flag expected in the command line)
        cases = [
            ("picks", {"force": False}, None),  # unforced picks needs no confirm
            ("official_mail", {}, None),  # can send, but is what the daemon runs unattended
            ("daily_admin_status", {}, None),
            ("test_email", {"confirm": True}, "--test-email"),
            ("picks", {"force": True, "confirm": True}, "--force"),
            ("baseline", {}, "--mode"),
        ]
        for kind, kwargs, flag in cases:
            with self.subTest(kind=kind, kwargs=kwargs), patch(RUN_CMD, return_value=(0, "{}")) as run_cmd:
                result = svc_ops.run_job(kind, root=self.root, **kwargs)
                self.assertTrue(result["ok"])
                run_cmd.assert_called()
                if flag:
                    self.assertIn(flag, run_cmd.call_args_list[0].args[0])

    def test_health_site_chains_three_steps_and_gates_diff_notify_on_snapshot_success(self):
        with patch(RUN_CMD, return_value=(0, "ok")) as run_cmd:
            svc_ops.run_job("health_site", root=self.root)
        self.assertEqual(
            [c.args[0][2] for c in run_cmd.call_args_list],
            ["quantcheck.health_watchdog", "quantcheck.site_snapshot", "quantcheck.site_diff_notify"],
        )

        calls = []

        def fake_run_cmd(args, timeout, capture_output=False):
            calls.append(args[2])
            return (1, "snapshot failed") if "site_snapshot" in args[2] else (0, "ok")

        with patch(RUN_CMD, side_effect=fake_run_cmd):
            result = svc_ops.run_job("health_site", root=self.root)
        self.assertEqual(calls, ["quantcheck.health_watchdog", "quantcheck.site_snapshot"])  # diff_notify must NOT run
        self.assertFalse(result["ok"])  # rc from the failed snapshot step propagates

    def test_official_mail_triggers_forced_picks_only_when_something_was_forwarded(self):
        def fake_run_cmd(args, timeout, capture_output=False):
            if "official_mail_forwarder" in args[2]:
                return (0, json.dumps({"forwarded": 2}))
            return (0, "picks ran")

        with patch(RUN_CMD, side_effect=fake_run_cmd) as run_cmd:
            svc_ops.run_job("official_mail", root=self.root)
        self.assertEqual([c.args[0][2] for c in run_cmd.call_args_list], ["quantcheck.official_mail_forwarder", "quantcheck.picks_check"])
        self.assertIn("--force", run_cmd.call_args_list[1].args[0])

        with patch(RUN_CMD, return_value=(0, json.dumps({"forwarded": 0}))) as run_cmd:
            svc_ops.run_job("official_mail", root=self.root)
        self.assertEqual(run_cmd.call_count, 1)

    def test_lock_held_skips_job_released_lock_runs_and_path_matches_scheduler(self):
        handle = self.hold_lock()
        with patch(RUN_CMD) as run_cmd:
            result = svc_ops.run_job("picks", root=self.root)
        self.assertEqual(result, {"kind": "picks", "skipped": "locked"})
        run_cmd.assert_not_called()

        handle.close()
        with patch(RUN_CMD, return_value=(0, "ok")):
            first = svc_ops.run_job("picks", root=self.root)
            second = svc_ops.run_job("picks", root=self.root)  # the lock is released after each job
        self.assertNotIn("skipped", first)
        self.assertNotIn("skipped", second)

        # The "reuses state/quantcheck.lock" contract: same file name scheduler.py locks.
        from quantcheck import scheduler as scheduler_mod

        self.assertEqual(scheduler_mod.LOCK_FILE.name, "quantcheck.lock")
        self.assertEqual(svc_ops._lock_path(self.root), self.root / "state" / "quantcheck.lock")


class StatusTests(ServiceOpsTestCase):
    def test_status_tolerates_missing_state_then_reports_health_dates_and_lock(self):
        empty = svc_ops.status(root=self.root)
        self.assertEqual(empty["health"], {})
        self.assertIsNone(empty["latest_pick_dates"]["monthly"])
        json.dumps(empty)

        self.write_json("state/health.json", {"last_run_at": "2026-08-31T00:00:00Z", "consecutive_failures": 0})
        self.write_json("state/latest_picks.json", {"monthly": {"pick_date": "Aug 2026"}, "weekly": {"pick_date": "Aug 28, 2026"}, "fetched_at": "2026-08-30T12:00:00Z"})
        result = svc_ops.status(root=self.root)
        self.assertEqual(result["health"]["consecutive_failures"], 0)
        self.assertEqual(result["latest_pick_dates"]["monthly"], "Aug 2026")
        self.assertIn("next_job", result)
        self.assertFalse(result["lock"]["held"])

        self.hold_lock()
        self.assertTrue(svc_ops.status(root=self.root)["lock"]["held"])

    def test_next_jobs_lists_every_job_sharing_the_slot_in_run_order(self):
        from zoneinfo import ZoneInfo

        target = datetime(2026, 10, 8, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("quantcheck.service.ops.scheduler_mod.next_due_jobs", return_value=(600, target, ["picks", "official_mail"])):
            result = svc_ops.status(root=self.root)
        self.assertEqual([job["kind"] for job in result["next_jobs"]], ["picks", "official_mail"])
        self.assertTrue(all(job["at"] == target.isoformat() and job["in_seconds"] == 600 for job in result["next_jobs"]))
        self.assertEqual(result["next_job"], {"kind": "picks", "at": target.isoformat(), "in_seconds": 600})  # backward compatible
        json.dumps(result)

        # A custom schedule with two kinds at the same minute exercises the real scheduler.next_due_jobs.
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\nQUANTCHECK_SCHEDULE=23:59:official_mail,23:59:picks\n", encoding="utf-8")
        real = svc_ops.status(root=self.root)
        self.assertEqual([job["kind"] for job in real["next_jobs"]], ["picks", "official_mail"])
        self.assertEqual(real["next_job"]["kind"], "picks")

    def test_scheduler_failure_keeps_status_alive(self):
        with patch("quantcheck.service.ops.scheduler_mod.next_due_jobs", side_effect=RuntimeError("boom")):
            result = svc_ops.status(root=self.root)
        self.assertIn("error", result["next_job"])
        self.assertEqual(result["next_jobs"], [])


class DiagnoseTests(ServiceOpsTestCase):
    def finding(self, result, check):
        return [f for f in result["findings"] if f["check"] == check][0]

    def test_missing_health_json_is_an_error_finding_with_doc_ref(self):
        result = svc_ops.diagnose(root=self.root)
        health = self.finding(result, "health_state")
        self.assertEqual(health["severity"], "error")
        self.assertIn("SITE_CHANGE_REPAIR.md", health["doc_ref"])
        self.assertEqual(result["overall"], "error")
        json.dumps(result)

    def test_healthy_state_produces_ok_overall(self):
        self.write_json("state/health.json", {"consecutive_failures": 0, "last_success_at": "2026-08-31T00:00:00Z"})
        self.write_json("state/raw/picks_raw_2026-08-31_000000.json", {})
        self.write_json("state/official_mail_forwarder_state.json", {})
        (self.root / "logs" / "email_delivery_ledger.jsonl").write_text(
            json.dumps({"recipient": "real@subscriber.io", "success": True}) + "\n", encoding="utf-8"
        )
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "SITE_CHANGE_REPAIR.md").write_text("# repair doc\n", encoding="utf-8")

        result = svc_ops.diagnose(root=self.root)

        self.assertEqual(result["overall"], "ok")
        severities = {f["check"]: f["severity"] for f in result["findings"]}
        self.assertEqual((severities["health_state"], severities["snapshot_freshness"]), ("ok", "ok"))

    def test_individual_problems_become_findings(self):
        def failing_health(root):
            self.write_json("state/health.json", {"consecutive_failures": 3, "last_error": "boom"}, root)

        def stale_snapshot(root):
            self.write_json("state/health.json", {"consecutive_failures": 0}, root)
            stale = self.write_json("state/raw/picks_raw_old.json", {}, root)
            old_time = (datetime.now() - timedelta(hours=48)).timestamp()
            os.utime(stale, (old_time, old_time))

        def ledger_failure(root):
            self.write_json("state/health.json", {"consecutive_failures": 0}, root)
            rows = [{"recipient": "real@subscriber.io", "success": True}, {"recipient": "real2@subscriber.io", "success": False}]
            (root / "logs" / "email_delivery_ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        def log_traceback(root):
            self.write_json("state/health.json", {"consecutive_failures": 0}, root)
            (root / "logs" / "quantgt_monitor.log").write_text("[t] ok\n[t] Traceback (most recent call last):\n", encoding="utf-8")

        def corrupt_store(root):
            self.write_json("state/health.json", {"consecutive_failures": 0}, root)
            (root / "state" / "memberships.json").write_text("{not valid", encoding="utf-8")

        cases = [
            ("failing health", failing_health, "health_state", "error"),
            ("stale snapshot", stale_snapshot, "snapshot_freshness", "warning"),
            ("delivery failures", ledger_failure, "delivery_ledger", "error"),
            ("traceback in log", log_traceback, "log_error_scan", "warning"),
            ("corrupt membership store", corrupt_store, "membership_store", "error"),
        ]
        for name, setup, check, severity in cases:
            with self.subTest(name):
                root = self.fresh_root()
                setup(root)
                finding = self.finding(svc_ops.diagnose(root=root), check)
                self.assertEqual(finding["severity"], severity)
                if check == "health_state":
                    self.assertIn("boom", finding["last_error"])
                if check == "log_error_scan":
                    self.assertEqual(finding["logs"][0]["log"], "quantgt_monitor.log")


class LogsAndDeliveriesTests(ServiceOpsTestCase):
    def test_logs_tail_grep_and_missing_file(self):
        log = self.root / "logs" / "quantcheck_scheduler.log"
        log.write_text("\n".join(f"line {i}" for i in range(200)) + "\n", encoding="utf-8")
        tail = svc_ops.logs("scheduler", lines=10, root=self.root)
        self.assertEqual(len(tail["lines"]), 10)
        self.assertEqual(tail["lines"][-1], "line 199")

        log.write_text("alpha\nbeta ERROR\ngamma\n", encoding="utf-8")
        self.assertEqual(svc_ops.logs("scheduler", lines=100, grep="error", root=self.root)["lines"], ["beta ERROR"])

        missing = svc_ops.logs("email", root=self.root)
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["lines"], [])

    def test_logs_rejects_unknown_traversal_and_bad_pattern(self):
        (self.root / "logs" / "quantcheck_scheduler.log").write_text("alpha\n", encoding="utf-8")
        for code, args, kwargs in [
            ("invalid_log_name", ("nonexistent",), {}),
            ("invalid_log_name", ("../../etc/passwd",), {}),
            ("invalid_grep_pattern", ("scheduler",), {"grep": "(unclosed"}),
        ]:
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ServiceError) as ctx:
                svc_ops.logs(*args, root=self.root, **kwargs)
            self.assertEqual(ctx.exception.code, code)

    def test_recent_deliveries_missing_ledger_filters_fixtures_and_respects_limit(self):
        ledger = self.root / "logs" / "email_delivery_ledger.jsonl"
        self.assertEqual(
            svc_ops.recent_deliveries(root=self.root),
            {"path": str(ledger), "count": 0, "filtered_fixture_count": 0, "deliveries": []},
        )

        rows = [{"recipient": r, "success": True} for r in ("real@subscriber.io", "a@example.com", "b@example.org", "c@example.net")]
        rows += [{"recipient": f"real{i}@subscriber.io", "success": True} for i in range(20)]
        ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        result = svc_ops.recent_deliveries(limit=50, root=self.root)
        self.assertEqual(result["count"], 21)
        self.assertEqual(result["filtered_fixture_count"], 3)
        self.assertNotIn("a@example.com", [d["recipient"] for d in result["deliveries"]])

        limited = svc_ops.recent_deliveries(limit=5, root=self.root)
        self.assertEqual(limited["count"], 5)
        self.assertEqual(limited["deliveries"][-1]["recipient"], "real19@subscriber.io")


class SchedulePreviewAndResendTests(ServiceOpsTestCase):
    def test_schedule_preview_days(self):
        result = svc_ops.schedule_preview(days=3, root=self.root)
        self.assertEqual(len(result["days"]), 3)
        for day in result["days"]:
            self.assertIn("jobs", day)
            self.assertIn("is_trading_day", day)
        json.dumps(result)
        self.assertEqual(svc_ops.schedule_preview(days=0, root=self.root)["days"], [])

    def test_historical_resend_preview_wraps_errors_and_returns_plan_summary(self):
        with patch("quantcheck.service.ops.prepare_resend", side_effect=svc_ops.ResendValidationError("no snapshot")):
            with self.assertRaises(ServiceError) as ctx:
                svc_ops.historical_resend_preview("Updated on Aug 7, 2026", root=self.root)
        self.assertEqual(ctx.exception.code, "resend_validation_failed")

        fake_plan = type("FakePlan", (), {"summary": lambda self: {"mode": "preview", "target_weekly_date": "x"}})()
        with patch("quantcheck.service.ops.prepare_resend", return_value=fake_plan):
            self.assertEqual(svc_ops.historical_resend_preview("x", root=self.root), {"mode": "preview", "target_weekly_date": "x"})

    def test_never_imports_execute_resend(self):
        # Structural guard: this module must not even import execute_resend, the
        # function capable of actually sending a historical resend.
        self.assertFalse(hasattr(svc_ops, "execute_resend"))


if __name__ == "__main__":
    unittest.main()
