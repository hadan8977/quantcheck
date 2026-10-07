import fcntl
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck.membership_store import Member, MembershipStore, save_store
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

    def write_json(self, relative: str, data) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class RunJobConfirmGateTests(ServiceOpsTestCase):
    def test_invalid_kind_raises(self):
        with self.assertRaises(ServiceError) as ctx:
            svc_ops.run_job("not_a_kind", root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_job_kind")

    def test_test_email_without_confirm_is_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            svc_ops.run_job("test_email", root=self.root)
        self.assertEqual(ctx.exception.code, "confirmation_required")

    def test_forced_picks_without_confirm_is_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            svc_ops.run_job("picks", force=True, root=self.root)
        self.assertEqual(ctx.exception.code, "confirmation_required")

    def test_unforced_picks_does_not_require_confirm(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")) as run_cmd:
            result = svc_ops.run_job("picks", force=False, root=self.root)
        self.assertTrue(result["ok"])
        run_cmd.assert_called_once()

    def test_official_mail_does_not_require_confirm_even_though_it_can_send(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "{}")) as run_cmd:
            result = svc_ops.run_job("official_mail", root=self.root)
        self.assertTrue(result["ok"])
        run_cmd.assert_called_once()

    def test_daily_admin_status_does_not_require_confirm(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")):
            result = svc_ops.run_job("daily_admin_status", root=self.root)
        self.assertTrue(result["ok"])


class RunJobDispatchTests(ServiceOpsTestCase):
    def test_test_email_with_confirm_dispatches_picks_check_test_email(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "sent")) as run_cmd:
            result = svc_ops.run_job("test_email", confirm=True, root=self.root)
        self.assertTrue(result["ok"])
        args = run_cmd.call_args[0][0]
        self.assertIn("--test-email", args)

    def test_forced_picks_with_confirm_dispatches_with_force_flag(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")) as run_cmd:
            svc_ops.run_job("picks", force=True, confirm=True, root=self.root)
        args = run_cmd.call_args[0][0]
        self.assertIn("--force", args)

    def test_baseline_dispatches_baseline_mode(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")) as run_cmd:
            svc_ops.run_job("baseline", root=self.root)
        args = run_cmd.call_args[0][0]
        self.assertIn("--mode", args)
        self.assertIn("baseline", args)

    def test_health_site_chains_three_steps_and_gates_diff_notify_on_snapshot_success(self):
        calls = []

        def fake_run_cmd(args, timeout, capture_output=False):
            calls.append(args)
            if "site_snapshot" in args[2]:
                return (1, "snapshot failed")  # simulate failure
            return (0, "ok")

        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", side_effect=fake_run_cmd):
            result = svc_ops.run_job("health_site", root=self.root)

        # site_diff_notify must NOT run when site_snapshot failed.
        modules_called = [c[2] for c in calls]
        self.assertIn("quantcheck.health_watchdog", modules_called)
        self.assertIn("quantcheck.site_snapshot", modules_called)
        self.assertNotIn("quantcheck.site_diff_notify", modules_called)
        self.assertFalse(result["ok"])  # rc from the failed snapshot step propagates

    def test_health_site_runs_diff_notify_when_snapshot_succeeds(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")) as run_cmd:
            svc_ops.run_job("health_site", root=self.root)
        modules_called = [c.args[0][2] for c in run_cmd.call_args_list]
        self.assertEqual(
            modules_called,
            ["quantcheck.health_watchdog", "quantcheck.site_snapshot", "quantcheck.site_diff_notify"],
        )

    def test_official_mail_triggers_forced_picks_check_when_something_was_forwarded(self):
        def fake_run_cmd(args, timeout, capture_output=False):
            if "official_mail_forwarder" in args[2]:
                return (0, json.dumps({"forwarded": 2}))
            return (0, "picks ran")

        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", side_effect=fake_run_cmd) as run_cmd:
            svc_ops.run_job("official_mail", root=self.root)

        modules_called = [c.args[0][2] for c in run_cmd.call_args_list]
        self.assertEqual(modules_called, ["quantcheck.official_mail_forwarder", "quantcheck.picks_check"])
        second_call_args = run_cmd.call_args_list[1].args[0]
        self.assertIn("--force", second_call_args)

    def test_official_mail_does_not_trigger_picks_when_nothing_forwarded(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, json.dumps({"forwarded": 0}))) as run_cmd:
            svc_ops.run_job("official_mail", root=self.root)
        self.assertEqual(run_cmd.call_count, 1)


class RunJobLockTests(ServiceOpsTestCase):
    def test_returns_skipped_locked_when_lock_is_held(self):
        lock_path = self.root / "state" / "quantcheck.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_handle = lock_path.open("a")
        self.addCleanup(held_handle.close)
        fcntl.flock(held_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with patch("quantcheck.service.ops.scheduler_mod.run_cmd") as run_cmd:
            result = svc_ops.run_job("picks", root=self.root)

        self.assertEqual(result, {"kind": "picks", "skipped": "locked"})
        run_cmd.assert_not_called()

    def test_lock_is_released_after_the_job_so_a_second_call_can_run(self):
        with patch("quantcheck.service.ops.scheduler_mod.run_cmd", return_value=(0, "ok")):
            svc_ops.run_job("picks", root=self.root)
            second = svc_ops.run_job("picks", root=self.root)
        self.assertNotIn("skipped", second)

    def test_uses_the_same_lock_file_scheduler_uses(self):
        # This is the actual "reuses state/quantcheck.lock" contract: the
        # path must match exactly what scheduler.py locks, not a
        # differently-named file, or the two would never contend.
        from quantcheck import scheduler as scheduler_mod

        with patch.dict("os.environ", {"QUANTCHECK_HOME": str(self.root)}):
            expected = self.root / "state" / "quantcheck.lock"
            # scheduler.LOCK_FILE is fixed at import time from the real
            # process env, so compare by relative shape instead of identity.
            self.assertEqual(scheduler_mod.LOCK_FILE.name, "quantcheck.lock")
            self.assertEqual(svc_ops._lock_path(self.root), expected)


class StatusTests(ServiceOpsTestCase):
    def test_status_reports_health_and_pick_dates(self):
        self.write_json("state/health.json", {"last_run_at": "2026-08-31T00:00:00Z", "consecutive_failures": 0})
        self.write_json("state/latest_picks.json", {"monthly": {"pick_date": "Aug 2026"}, "weekly": {"pick_date": "Aug 28, 2026"}, "fetched_at": "2026-08-30T12:00:00Z"})

        result = svc_ops.status(root=self.root)

        self.assertEqual(result["health"]["consecutive_failures"], 0)
        self.assertEqual(result["latest_pick_dates"]["monthly"], "Aug 2026")
        self.assertIn("next_job", result)
        self.assertIn("lock", result)
        self.assertFalse(result["lock"]["held"])

    def test_status_reports_lock_held(self):
        lock_path = self.root / "state" / "quantcheck.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = svc_ops.status(root=self.root)
        self.assertTrue(result["lock"]["held"])

    def test_status_tolerates_missing_state_files(self):
        result = svc_ops.status(root=self.root)
        self.assertEqual(result["health"], {})
        self.assertIsNone(result["latest_pick_dates"]["monthly"])

    def test_status_is_json_serializable(self):
        result = svc_ops.status(root=self.root)
        json.dumps(result)


class DiagnoseTests(ServiceOpsTestCase):
    def test_missing_health_json_is_an_error_finding_with_doc_ref(self):
        result = svc_ops.diagnose(root=self.root)
        health_findings = [f for f in result["findings"] if f["check"] == "health_state"]
        self.assertEqual(health_findings[0]["severity"], "error")
        self.assertIn("SITE_CHANGE_REPAIR.md", health_findings[0]["doc_ref"])
        self.assertEqual(result["overall"], "error")

    def test_healthy_state_produces_ok_overall(self):
        self.write_json("state/health.json", {"consecutive_failures": 0, "last_success_at": "2026-08-31T00:00:00Z"})
        raw_dir = self.root / "state" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "picks_raw_2026-08-31_000000.json").write_text("{}", encoding="utf-8")
        self.write_json("state/official_mail_forwarder_state.json", {})
        (self.root / "logs" / "email_delivery_ledger.jsonl").write_text(
            json.dumps({"recipient": "real@subscriber.io", "success": True}) + "\n", encoding="utf-8"
        )
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "SITE_CHANGE_REPAIR.md").write_text("# repair doc\n", encoding="utf-8")

        result = svc_ops.diagnose(root=self.root)

        self.assertEqual(result["overall"], "ok")
        severities = {f["check"]: f["severity"] for f in result["findings"]}
        self.assertEqual(severities["health_state"], "ok")
        self.assertEqual(severities["snapshot_freshness"], "ok")

    def test_consecutive_failures_is_an_error_finding(self):
        self.write_json("state/health.json", {"consecutive_failures": 3, "last_error": "boom"})
        result = svc_ops.diagnose(root=self.root)
        health_findings = [f for f in result["findings"] if f["check"] == "health_state"]
        self.assertEqual(health_findings[0]["severity"], "error")
        self.assertIn("boom", health_findings[0]["last_error"])

    def test_stale_snapshot_is_a_warning(self):
        self.write_json("state/health.json", {"consecutive_failures": 0})
        raw_dir = self.root / "state" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        stale = raw_dir / "picks_raw_old.json"
        stale.write_text("{}", encoding="utf-8")
        old_time = (datetime.now() - timedelta(hours=48)).timestamp()
        import os

        os.utime(stale, (old_time, old_time))

        result = svc_ops.diagnose(root=self.root)
        freshness = [f for f in result["findings"] if f["check"] == "snapshot_freshness"][0]
        self.assertEqual(freshness["severity"], "warning")

    def test_delivery_ledger_failures_are_an_error_finding(self):
        self.write_json("state/health.json", {"consecutive_failures": 0})
        ledger = self.root / "logs" / "email_delivery_ledger.jsonl"
        with ledger.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"recipient": "real@subscriber.io", "success": True}) + "\n")
            handle.write(json.dumps({"recipient": "real2@subscriber.io", "success": False}) + "\n")

        result = svc_ops.diagnose(root=self.root)
        ledger_finding = [f for f in result["findings"] if f["check"] == "delivery_ledger"][0]
        self.assertEqual(ledger_finding["severity"], "error")

    def test_log_error_scan_finds_error_lines(self):
        self.write_json("state/health.json", {"consecutive_failures": 0})
        (self.root / "logs" / "quantgt_monitor.log").write_text("[t] ok\n[t] Traceback (most recent call last):\n", encoding="utf-8")

        result = svc_ops.diagnose(root=self.root)
        scan = [f for f in result["findings"] if f["check"] == "log_error_scan"][0]
        self.assertEqual(scan["severity"], "warning")
        self.assertEqual(scan["logs"][0]["log"], "quantgt_monitor.log")

    def test_membership_store_corruption_is_reported(self):
        self.write_json("state/health.json", {"consecutive_failures": 0})
        store_path = self.root / "state" / "memberships.json"
        store_path.write_text("{not valid", encoding="utf-8")

        result = svc_ops.diagnose(root=self.root)
        membership_finding = [f for f in result["findings"] if f["check"] == "membership_store"][0]
        self.assertEqual(membership_finding["severity"], "error")

    def test_result_is_json_serializable(self):
        result = svc_ops.diagnose(root=self.root)
        json.dumps(result)


class LogsTests(ServiceOpsTestCase):
    def test_reads_tail_of_allowed_log(self):
        (self.root / "logs" / "quantcheck_scheduler.log").write_text("\n".join(f"line {i}" for i in range(200)) + "\n", encoding="utf-8")
        result = svc_ops.logs("scheduler", lines=10, root=self.root)
        self.assertEqual(len(result["lines"]), 10)
        self.assertEqual(result["lines"][-1], "line 199")

    def test_unknown_log_name_raises(self):
        with self.assertRaises(ServiceError) as ctx:
            svc_ops.logs("nonexistent", root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_log_name")

    def test_path_traversal_style_name_is_rejected(self):
        with self.assertRaises(ServiceError):
            svc_ops.logs("../../etc/passwd", root=self.root)

    def test_grep_filters_lines(self):
        (self.root / "logs" / "quantcheck_scheduler.log").write_text("alpha\nbeta ERROR\ngamma\n", encoding="utf-8")
        result = svc_ops.logs("scheduler", lines=100, grep="error", root=self.root)
        self.assertEqual(result["lines"], ["beta ERROR"])

    def test_invalid_grep_pattern_raises(self):
        (self.root / "logs" / "quantcheck_scheduler.log").write_text("alpha\n", encoding="utf-8")
        with self.assertRaises(ServiceError) as ctx:
            svc_ops.logs("scheduler", grep="(unclosed", root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_grep_pattern")

    def test_missing_log_file_returns_empty_not_an_error(self):
        result = svc_ops.logs("email", root=self.root)
        self.assertFalse(result["exists"])
        self.assertEqual(result["lines"], [])


class RecentDeliveriesTests(ServiceOpsTestCase):
    def test_filters_fixture_recipients(self):
        ledger = self.root / "logs" / "email_delivery_ledger.jsonl"
        with ledger.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"recipient": "real@subscriber.io", "success": True}) + "\n")
            handle.write(json.dumps({"recipient": "a@example.com", "success": True}) + "\n")
            handle.write(json.dumps({"recipient": "b@example.org", "success": True}) + "\n")
            handle.write(json.dumps({"recipient": "c@example.net", "success": True}) + "\n")

        result = svc_ops.recent_deliveries(limit=50, root=self.root)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deliveries"][0]["recipient"], "real@subscriber.io")
        self.assertEqual(result["filtered_fixture_count"], 3)

    def test_missing_ledger_returns_empty(self):
        result = svc_ops.recent_deliveries(root=self.root)
        self.assertEqual(result, {"path": str(self.root / "logs" / "email_delivery_ledger.jsonl"), "count": 0, "filtered_fixture_count": 0, "deliveries": []})

    def test_respects_limit(self):
        ledger = self.root / "logs" / "email_delivery_ledger.jsonl"
        with ledger.open("w", encoding="utf-8") as handle:
            for i in range(20):
                handle.write(json.dumps({"recipient": f"real{i}@subscriber.io", "success": True}) + "\n")

        result = svc_ops.recent_deliveries(limit=5, root=self.root)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["deliveries"][-1]["recipient"], "real19@subscriber.io")


class SchedulePreviewTests(ServiceOpsTestCase):
    def test_returns_requested_number_of_days(self):
        result = svc_ops.schedule_preview(days=3, root=self.root)
        self.assertEqual(len(result["days"]), 3)
        for day in result["days"]:
            self.assertIn("jobs", day)
            self.assertIn("is_trading_day", day)

    def test_zero_days_returns_empty(self):
        result = svc_ops.schedule_preview(days=0, root=self.root)
        self.assertEqual(result["days"], [])

    def test_is_json_serializable(self):
        result = svc_ops.schedule_preview(days=2, root=self.root)
        json.dumps(result)


class HistoricalResendPreviewTests(ServiceOpsTestCase):
    def test_wraps_validation_error_as_service_error(self):
        with patch("quantcheck.service.ops.prepare_resend", side_effect=svc_ops.ResendValidationError("no snapshot")):
            with self.assertRaises(ServiceError) as ctx:
                svc_ops.historical_resend_preview("Updated on Aug 7, 2026", root=self.root)
        self.assertEqual(ctx.exception.code, "resend_validation_failed")

    def test_returns_plan_summary_on_success(self):
        fake_plan = type("FakePlan", (), {"summary": lambda self: {"mode": "preview", "target_weekly_date": "x"}})()
        with patch("quantcheck.service.ops.prepare_resend", return_value=fake_plan):
            result = svc_ops.historical_resend_preview("x", root=self.root)
        self.assertEqual(result, {"mode": "preview", "target_weekly_date": "x"})

    def test_never_imports_execute_resend(self):
        # Structural guard: this module must not even import execute_resend,
        # the function capable of actually sending a historical resend. The
        # module docstring is allowed to *mention* it in prose (explaining
        # why it is avoided); what must never happen is it becoming callable
        # from here.
        self.assertFalse(hasattr(svc_ops, "execute_resend"))



class StatusNextJobsTests(ServiceOpsTestCase):
    def test_next_jobs_lists_every_job_sharing_the_slot_in_run_order(self):
        from zoneinfo import ZoneInfo

        target = datetime(2026, 10, 8, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("quantcheck.service.ops.scheduler_mod.next_due_jobs", return_value=(600, target, ["picks", "official_mail"])):
            result = svc_ops.status(root=self.root)
        self.assertEqual([job["kind"] for job in result["next_jobs"]], ["picks", "official_mail"])
        self.assertTrue(all(job["at"] == target.isoformat() and job["in_seconds"] == 600 for job in result["next_jobs"]))
        self.assertEqual(result["next_job"], {"kind": "picks", "at": target.isoformat(), "in_seconds": 600})  # backward compatible
        json.dumps(result)

    def test_real_scheduler_colliding_slot(self):
        # A custom schedule with two kinds at the same minute exercises the
        # real scheduler.next_due_jobs (no patching) through status().
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\nQUANTCHECK_SCHEDULE=23:59:official_mail,23:59:picks\n", encoding="utf-8")
        result = svc_ops.status(root=self.root)
        self.assertEqual([job["kind"] for job in result["next_jobs"]], ["picks", "official_mail"])
        self.assertEqual(result["next_job"]["kind"], "picks")

    def test_scheduler_failure_keeps_status_alive(self):
        with patch("quantcheck.service.ops.scheduler_mod.next_due_jobs", side_effect=RuntimeError("boom")):
            result = svc_ops.status(root=self.root)
        self.assertIn("error", result["next_job"])
        self.assertEqual(result["next_jobs"], [])


if __name__ == "__main__":
    unittest.main()
