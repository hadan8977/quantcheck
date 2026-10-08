import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from quantcheck.membership import MEMBERSHIP_TZ
from quantcheck.membership_store import Member, MembershipStore, load_store, save_store
from quantcheck.service import ServiceError
from quantcheck.service import members as svc


def ny(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=MEMBERSHIP_TZ)


class ServiceMembersTestCase(unittest.TestCase):
    """All state lives under a tmp root; nothing here ever touches the real
    /opt/quantcheck state/ or logs/ directories.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\n", encoding="utf-8")
        self.subscriber_file = self.root / "notify_recipients.txt"

    def write_subscribers(self, *emails):
        self.subscriber_file.write_text("\n".join(emails) + ("\n" if emails else ""), encoding="utf-8")

    def store_path(self) -> Path:
        return self.root / "state" / "memberships.json"

    def write_store(self, members):
        store = MembershipStore(path=self.store_path(), members=list(members))
        save_store(store, backup=False)

    def read_store(self) -> MembershipStore:
        return load_store(self.store_path())

    def snapshot(self):
        """Every file under the tmp root (path -> bytes), including any .bak files."""
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)


class ListGetAndErrorTests(ServiceMembersTestCase):
    def test_list_members_empty_status_filter_and_expiring_window(self):
        self.assertEqual(svc.list_members(root=self.root), {"count": 0, "members": []})  # no store yet: not an error

        now = datetime.now(MEMBERSHIP_TZ)
        self.write_store(
            [
                Member(email="soon@example.com", status="active", joined_at=now - timedelta(days=10), expires_at=now + timedelta(days=5)),
                Member(email="later@example.com", status="active", joined_at=now - timedelta(days=10), expires_at=now + timedelta(days=90)),
                Member(email="expired@example.com", status="active", joined_at=now - timedelta(days=100), expires_at=now - timedelta(days=1)),
            ]
        )
        by_status = svc.list_members(status="expired", root=self.root)
        self.assertEqual([m["email"] for m in by_status["members"]], ["expired@example.com"])
        self.assertEqual(by_status["count"], 1)
        expiring = svc.list_members(expiring_within_days=14, root=self.root)
        self.assertEqual([m["email"] for m in expiring["members"]], ["soon@example.com"])

    def test_get_member_is_case_insensitive_and_returns_history(self):
        member = Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))
        member.add_history(action="add", months=1, expires_at=ny(2099, 1, 1), actor="test", at=ny(2026, 1, 1))
        self.write_store([member])
        result = svc.get_member("A@Example.com", root=self.root)
        self.assertEqual(result["member"]["email"], "a@example.com")
        self.assertEqual(len(result["member"]["history"]), 1)

    def test_service_errors_carry_stable_codes(self):
        self.write_store([Member(email="dup@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))])
        cases = [
            ("invalid_status", svc.list_members, (), {"status": "bogus"}),
            ("member_not_found", svc.get_member, ("nobody@example.com",), {}),
            ("member_not_found", svc.extend_member, ("nobody@example.com", 1), {}),
            ("member_not_found", svc.set_expiry, ("nobody@example.com", "2026-12-09", "n/a"), {}),
            ("member_not_found", svc.remove_member, ("nobody@example.com", "n/a"), {}),
            ("member_not_found", svc.record_payment, ("nobody@example.com", 5), {}),
            ("invalid_payment", svc.record_payment, ("dup@example.com", None), {}),
            ("invalid_email", svc.add_member, ("not-an-email", 1), {}),
            ("member_already_exists", svc.add_member, ("DUP@example.com", 1), {"joined_at": "2026-08-10"}),
            ("invalid_months", svc.add_member, ("new@example.com", 0), {}),
            ("invalid_expiry", svc.add_member, ("new@example.com",), {"expires_at": "null"}),
            ("invalid_expiry", svc.migrate_from_recipients, (None, "migrated"), {"dry_run": True}),
            ("invalid_date", svc.payments_report, (), {"since": "soon"}),
        ]
        for code, fn, args, kwargs in cases:
            with self.subTest(code=code, fn=fn.__name__, args=args):
                self.assert_code(code, fn, *args, root=self.root, **kwargs)

    def test_corrupt_store_raises_service_error_not_fail_open(self):
        # Unlike notify_routes.subscriber_recipients, admin-facing reads
        # must surface storage corruption loudly, not silently paper over it.
        self.store_path().write_text("{not json", encoding="utf-8")
        self.assert_code("membership_store_error", svc.list_members, root=self.root)


class AddExtendSetExpiryRemoveTests(ServiceMembersTestCase):
    def test_add_member_computes_expiry_syncs_recipients_and_writes_history(self):
        self.write_subscribers("existing@example.com")
        # Pin "now" so the 2026-09-09 expiry is still in the future.
        with patch.object(svc, "_now", return_value=ny(2026, 8, 10)):
            result = svc.add_member("new@example.com", 1, note="test add", joined_at="2026-08-10", root=self.root)
            member = result["member"]
            self.assertEqual(member["expires_at"], ny(2026, 9, 9).isoformat())
            self.assertEqual((member["email"], member["status"], member["months_total"]), ("new@example.com", "active", 1))
            contents = self.subscriber_file.read_text(encoding="utf-8")
            self.assertIn("existing@example.com", contents)
            self.assertIn("new@example.com", contents)
            self.assertEqual(len(list(self.root.glob("notify_recipients.txt.*.bak"))), 1)  # existing file was backed up

            svc.add_member("three@example.com", 3, note="paid 3mo", joined_at="2026-08-10", root=self.root)

            # A missing recipients file is created on demand (nothing to back up).
            self.subscriber_file.unlink()
            svc.add_member("fresh@example.com", 1, joined_at="2026-08-10", root=self.root)
            self.assertEqual(self.subscriber_file.read_text(encoding="utf-8").count("fresh@example.com"), 1)
        history = self.read_store().find("three@example.com").history
        self.assertEqual([(h["action"], h["months"]) for h in history], [("add", 3)])

    def test_extend_stacks_accumulates_months_and_reinstates_cancelled(self):
        self.write_subscribers()  # empty: as if remove_member had taken them out
        self.write_store(
            [
                Member(email="a@example.com", status="active", joined_at=ny(2026, 8, 10), expires_at=ny(2026, 9, 9)),
                Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1)),
            ]
        )
        first = svc.extend_member("a@example.com", 3, root=self.root)
        self.assertEqual(first["member"]["expires_at"], ny(2026, 12, 9).isoformat())  # stacks on current expiry
        self.assertEqual(first["member"]["months_total"], 3)
        svc.extend_member("a@example.com", 2, root=self.root)
        self.assertEqual(self.read_store().find("a@example.com").months_total, 5)

        reinstated = svc.extend_member("gone@example.com", 1, root=self.root)
        self.assertEqual(reinstated["member"]["stored_status"], "active")
        self.assertIn("gone@example.com", self.subscriber_file.read_text(encoding="utf-8"))

    def test_set_expiry_explicit_null_and_cancelled_stays_cancelled(self):
        self.write_store(
            [
                Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 9, 9)),
                Member(email="legacy@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 9, 9)),
                Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1)),
            ]
        )
        explicit = svc.set_expiry("a@example.com", "2026-12-09", "fixed typo from 09-09", root=self.root)
        self.assertEqual(explicit["member"]["expires_at"], ny(2026, 12, 9).isoformat())
        self.assertEqual(self.read_store().find("a@example.com").history[-1]["reason"], "fixed typo from 09-09")

        null = svc.set_expiry("legacy@example.com", None, "grandfathered", root=self.root)
        self.assertIsNone(null["member"]["expires_at"])
        self.assertEqual(null["member"]["status"], "legacy")

        # set_expiry is a pure date-correction tool; it must not un-cancel someone (use extend_member).
        cancelled = svc.set_expiry("gone@example.com", "2099-01-01", "typo fix", root=self.root)
        self.assertEqual((cancelled["member"]["stored_status"], cancelled["member"]["status"]), ("cancelled", "cancelled"))

    def test_remove_marks_cancelled_removes_from_file_and_keeps_record_and_history(self):
        self.write_subscribers("a@example.com", "b@example.com")
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))])

        result = svc.remove_member("a@example.com", "chargeback", root=self.root)

        self.assertEqual((result["member"]["stored_status"], result["member"]["status"]), ("cancelled", "cancelled"))
        contents = self.subscriber_file.read_text(encoding="utf-8")
        self.assertNotIn("a@example.com", contents)
        self.assertIn("b@example.com", contents)
        member = self.read_store().find("a@example.com")  # removed but not forgotten
        self.assertEqual((member.history[-1]["action"], member.history[-1]["reason"]), ("remove", "chargeback"))


class ReportAndMigrationTests(ServiceMembersTestCase):
    def test_expiring_report_counts_by_bucket(self):
        now = datetime.now(MEMBERSHIP_TZ)
        joined = now - timedelta(days=100)
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=joined, expires_at=now + timedelta(days=100)),
                Member(email="soon@example.com", status="active", joined_at=joined, expires_at=now + timedelta(days=3)),
                Member(email="expired@example.com", status="active", joined_at=joined, expires_at=now - timedelta(days=1)),
                Member(email="cancelled@example.com", status="cancelled", joined_at=joined, expires_at=now + timedelta(days=100)),
                Member(email="legacy@example.com", status="legacy", joined_at=joined, expires_at=None),
            ]
        )
        report = svc.expiring_report(within_days=14, root=self.root)
        self.assertEqual(
            {k: report[k] for k in ("expired_count", "cancelled_count", "legacy_count", "expiring_count", "active_count")},
            # "active" includes soon-to-expire (expiring is a subset of active).
            {"expired_count": 1, "cancelled_count": 1, "legacy_count": 1, "expiring_count": 1, "active_count": 2},
        )
        self.assertEqual(report["expiring"][0]["email"], "soon@example.com")

    def test_sync_recipients_reports_drift_read_only_then_in_sync(self):
        self.write_subscribers("in_file_only@example.com", "active@example.com")
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1)),
                Member(email="missing_from_file@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1)),
            ]
        )
        before = self.snapshot()

        result = svc.sync_recipients(root=self.root)

        self.assertEqual(result["drift"]["in_file_unknown_to_store"], ["in_file_only@example.com"])
        self.assertEqual(result["drift"]["active_members_missing_from_file"], ["missing_from_file@example.com"])
        self.assertFalse(result["in_sync"])
        self.assertEqual(self.snapshot(), before)  # read-only: byte-identical

        self.write_subscribers("active@example.com", "missing_from_file@example.com")
        self.assertTrue(svc.sync_recipients(root=self.root)["in_sync"])

    def test_migrate_dry_run_reports_plan_and_writes_nothing(self):
        self.write_subscribers("a@example.com", "b@example.com")
        before = self.snapshot()
        result = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=True, root=self.root)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["to_migrate_count"], 2)
        self.assertEqual(sorted(result["to_migrate"]), ["a@example.com", "b@example.com"])
        self.assertFalse(self.store_path().exists())
        self.assertEqual(self.snapshot(), before)  # byte-identical: no store, no .bak

    def test_migrate_execute_creates_members_backs_up_and_rerun_is_idempotent(self):
        self.write_subscribers("a@example.com", "b@example.com")
        note = "migrated from notify_recipients.txt on 2026-08-31"
        first = svc.migrate_from_recipients("2026-10-09", note, dry_run=False, root=self.root)

        self.assertEqual(first["migrated_count"], 2)
        self.assertEqual(len(list(self.root.glob("notify_recipients.txt.*.bak"))), 1)
        store = self.read_store()
        self.assertEqual(len(store.members), 2)
        member = store.find("a@example.com")
        self.assertEqual((member.expires_at, member.status, member.note), (ny(2026, 10, 9), "active", note))
        self.assertEqual((member.history[0]["action"], member.history[0]["actor"]), ("migrate", "migration"))

        # A manual correction in between runs must survive a re-run untouched.
        svc.set_expiry("a@example.com", "2026-11-09", "actually paid through November", root=self.root)
        second = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=False, root=self.root)
        self.assertEqual((second["migrated_count"], second["already_migrated_count"]), (0, 2))
        store = self.read_store()
        self.assertEqual(len(store.members), 2)  # no duplicates
        self.assertEqual(store.find("a@example.com").expires_at, ny(2026, 11, 9))
        self.assertEqual(store.find("b@example.com").expires_at, ny(2026, 10, 9))


NOW = ny(2026, 10, 7, 12)
ALIGNED = ny(2026, 11, 1)


class BulkTestCase(ServiceMembersTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(svc, "_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def active(self, email, expires_at=ALIGNED, **kwargs):
        return Member(email=email, status="active", joined_at=ny(2026, 8, 1), expires_at=expires_at, **kwargs)


class AddMemberExpiryModeTests(BulkTestCase):
    def test_exactly_one_of_months_expires_at_align_is_required(self):
        self.write_subscribers()
        for kwargs in ({}, {"months": 1, "expires_at": "2026-11-01"}, {"months": 1, "align": True}, {"expires_at": "2026-11-01", "align": True}):
            with self.subTest(kwargs=kwargs):
                self.assert_code("invalid_arguments", svc.add_member, "new@example.com", root=self.root, **kwargs)
        self.assertFalse(self.store_path().exists())

    def test_expires_at_and_months_modes_are_recorded_in_history(self):
        self.write_subscribers()
        member = svc.add_member("new@example.com", expires_at="2026-11-01", root=self.root)["member"]
        self.assertEqual((member["expires_at"], member["months_total"]), (ALIGNED.isoformat(), 0))
        entry = member["history"][-1]
        self.assertEqual((entry["action"], entry["months"], entry["mode"], entry["expires_at"]), ("add", None, "expires_at", ALIGNED.isoformat()))
        self.assertIn("new@example.com", self.subscriber_file.read_text(encoding="utf-8"))

        entry = svc.add_member("monthly@example.com", 1, root=self.root)["member"]["history"][-1]
        self.assertEqual((entry["months"], entry["mode"]), (1, "months"))

    def test_align_picks_the_most_common_active_expiry_and_needs_active_members(self):
        self.write_subscribers()
        other = ny(2026, 10, 9)
        old = Member(email="old@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 3, 1))
        self.write_store(
            [self.active("a@example.com"), self.active("b@example.com"), self.active("c@example.com", other), old]
            + [Member(email=f"x{i}@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2027, 1, 1)) for i in range(3)]
        )
        result = svc.add_member("new@example.com", align=True, root=self.root)
        self.assertEqual(result["member"]["expires_at"], ALIGNED.isoformat())
        self.assertEqual(result["member"]["history"][-1]["mode"], "align")
        self.assertEqual(result["member"]["months_total"], 0)

        self.write_store([old])
        self.assert_code("no_active_members", svc.add_member, "new@example.com", align=True, root=self.root)


class ParseEmailListTests(unittest.TestCase):
    def test_parse_email_list(self):
        text = "a@example.com, b@example.com;c@example.com\n  d@example.com\te@example.com # trailing comment f@example.com\n# whole line\ng@example.com"
        self.assertEqual(
            svc.parse_email_list(text),
            ["a@example.com", "b@example.com", "c@example.com", "d@example.com", "e@example.com", "g@example.com"],
        )
        self.assertEqual(svc.parse_email_list(["a@example.com b@example.com", "c@example.com"]), ["a@example.com", "b@example.com", "c@example.com"])
        self.assertEqual(svc.parse_email_list(None), [])


class BulkAddTests(BulkTestCase):
    def test_dry_run_is_default_and_writes_nothing(self):
        self.write_subscribers("keep@example.com")
        self.write_store([self.active("a@example.com")])
        before = self.snapshot()
        result = svc.bulk_add("n1@example.com n2@example.com", expires_at="2026-11-01", root=self.root)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["counts"]["ok"], 2)
        self.assertEqual(result["recipients_file"]["added"], ["n1@example.com", "n2@example.com"])
        self.assertFalse(result["recipients_file"]["changed"])
        self.assertEqual(self.snapshot(), before)  # store, recipients file and no stray .bak files: byte-identical

    def test_apply_writes_store_and_recipients_once(self):
        self.write_subscribers("keep@example.com")
        self.write_store([self.active("a@example.com")])
        with patch.object(svc, "_save_store", wraps=svc._save_store) as save, patch.object(
            svc.recipients, "write_recipient_file", wraps=svc.recipients.write_recipient_file
        ) as write:
            result = svc.bulk_add(["n1@example.com", "n2@example.com", "n3@example.com"], align=True, dry_run=False, root=self.root)
        self.assertEqual((save.call_count, write.call_count), (1, 1))
        self.assertEqual((result["counts"]["ok"], result["mode"]), (3, "align"))
        store = self.read_store()
        for email in ("n1@example.com", "n2@example.com", "n3@example.com"):
            self.assertEqual((store.find(email).expires_at, store.find(email).months_total), (ALIGNED, 0))
        lines = self.subscriber_file.read_text(encoding="utf-8").splitlines()
        for email in ("keep@example.com", "n1@example.com", "n2@example.com", "n3@example.com"):
            self.assertIn(email, lines)

    def test_invalid_existing_and_duplicates_are_reported_not_fatal(self):
        self.write_subscribers()
        self.write_store([self.active("a@example.com")])
        result = svc.bulk_add(
            "new@example.com, NEW@example.com, not-an-email, a@example.com, junk",
            expires_at="2026-11-01",
            dry_run=False,
            root=self.root,
        )
        by_email = {r["email"]: r for r in result["results"]}
        self.assertEqual(by_email["new@example.com"]["outcome"], "ok")
        self.assertEqual(by_email["not-an-email"]["code"], "invalid_email")
        self.assertEqual(by_email["junk"]["code"], "invalid_email")
        self.assertEqual(by_email["a@example.com"]["code"], "already_member")
        self.assertEqual(result["counts"]["requested"], 5)
        self.assertEqual(result["counts"]["duplicates_ignored"], 1)
        self.assertEqual(result["counts"]["ok"], 1)
        self.assertEqual(result["counts"]["skipped_by_code"], {"already_member": 1, "invalid_email": 2})
        self.assertEqual(self.read_store().find("a@example.com").history, [])  # existing member untouched

    def test_months_mode_records_payment_per_member_and_nothing_to_add_writes_nothing(self):
        self.write_subscribers()
        result = svc.bulk_add(
            "a@example.com b@example.com", months=1, payment={"amount": 99, "currency": "cny", "channel": "WeChat"}, dry_run=False, root=self.root
        )
        self.assertEqual(result["counts"]["ok"], 2)
        for email in ("a@example.com", "b@example.com"):
            member = self.read_store().find(email)
            self.assertEqual(member.expires_at, ny(2026, 10, 9))  # next anchor is only 2 days away
            self.assertEqual(member.months_total, 1)
            self.assertEqual(member.history[-1]["payment"]["currency"], "CNY")
            self.assertEqual(member.history[-1]["payment"]["channel"], "wechat")

        before = self.snapshot()
        again = svc.bulk_add("a@example.com", expires_at="2026-11-01", dry_run=False, root=self.root)
        self.assertEqual(again["counts"]["ok"], 0)
        self.assertEqual(self.snapshot(), before)


class BulkExtendTests(BulkTestCase):
    def test_dry_run_default_writes_nothing_and_shows_new_expiry(self):
        self.write_subscribers("a@example.com", "b@example.com")
        self.write_store([self.active("a@example.com"), self.active("b@example.com")])
        before = self.snapshot()
        result = svc.bulk_extend("a@example.com b@example.com", months=1, root=self.root)
        self.assertTrue(result["dry_run"])
        self.assertEqual([r["expires_at"] for r in result["results"]], [ny(2026, 12, 1).isoformat()] * 2)
        self.assertEqual(self.snapshot(), before)

    def test_apply_matches_extend_member_semantics_and_saves_once(self):
        self.write_subscribers("a@example.com")
        gone = Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 2, 9), months_total=1)
        self.write_store([self.active("a@example.com", months_total=2), gone])
        with patch.object(svc, "_save_store", wraps=svc._save_store) as save, patch.object(
            svc.recipients, "write_recipient_file", wraps=svc.recipients.write_recipient_file
        ) as write:
            result = svc.bulk_extend("a@example.com gone@example.com nobody@example.com bad", months=1, note="renewal", dry_run=False, root=self.root)
        self.assertEqual((save.call_count, write.call_count), (1, 1))
        store = self.read_store()
        self.assertEqual(store.find("a@example.com").expires_at, ny(2026, 12, 1))  # stacked on current expiry
        self.assertEqual(store.find("a@example.com").months_total, 3)
        reinstated = store.find("gone@example.com")
        self.assertEqual(reinstated.status, "active")
        self.assertEqual(reinstated.expires_at, ny(2026, 10, 9))  # expired -> restarts from now (next anchor)
        self.assertEqual(reinstated.note, "renewal")
        self.assertIn("gone@example.com", self.subscriber_file.read_text(encoding="utf-8"))
        codes = {r["email"]: r["code"] for r in result["results"]}
        self.assertEqual(codes["nobody@example.com"], "member_not_found")
        self.assertEqual(codes["bad"], "invalid_email")
        self.assertEqual(result["counts"]["ok"], 2)
        self.assertEqual([r["email"] for r in result["results"]][:2], ["a@example.com", "gone@example.com"])  # input order kept

    def test_all_active_only_touches_active_members(self):
        self.write_subscribers()
        self.write_store(
            [
                self.active("a@example.com"),
                self.active("b@example.com"),
                Member(email="old@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 3, 1)),
                Member(email="legacy@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=None),
                Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2027, 1, 1)),
            ]
        )
        result = svc.bulk_extend(months=1, all_active=True, dry_run=False, root=self.root)
        self.assertEqual(sorted(r["email"] for r in result["results"]), ["a@example.com", "b@example.com"])
        store = self.read_store()
        self.assertEqual(store.find("old@example.com").expires_at, ny(2026, 3, 1))
        self.assertIsNone(store.find("legacy@example.com").expires_at)
        self.assertEqual(store.find("gone@example.com").status, "cancelled")


class BulkSetExpiryTests(BulkTestCase):
    def test_dry_run_then_apply(self):
        self.write_subscribers("a@example.com")
        self.write_store([self.active("a@example.com", ny(2026, 10, 9)), self.active("b@example.com", ny(2026, 10, 9))])
        before = self.snapshot()
        dry = svc.bulk_set_expiry("a@example.com b@example.com missing@example.com", expires_at="2026-11-01", root=self.root)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dry["results"][0]["previous_expires_at"], ny(2026, 10, 9).isoformat())

        with patch.object(svc, "_save_store", wraps=svc._save_store) as save:
            applied = svc.bulk_set_expiry("a@example.com b@example.com missing@example.com", expires_at="2026-11-01", note="align", dry_run=False, root=self.root)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(applied["counts"]["ok"], 2)
        self.assertEqual(applied["counts"]["skipped_by_code"], {"member_not_found": 1})
        store = self.read_store()
        self.assertEqual(store.find("a@example.com").expires_at, ALIGNED)
        self.assertEqual(store.find("a@example.com").history[-1]["reason"], "align")
        self.assertEqual(self.subscriber_file.read_text(encoding="utf-8").count("b@example.com"), 0)  # recipients file untouched

    def test_all_active_skips_expired_and_never_changes_cancelled_status(self):
        self.write_store(
            [
                self.active("a@example.com", ny(2026, 10, 9)),
                Member(email="old@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 3, 1)),
                Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 2, 9)),
            ]
        )
        result = svc.bulk_set_expiry(expires_at="2026-11-01", all_active=True, dry_run=False, root=self.root)
        self.assertEqual([r["email"] for r in result["results"]], ["a@example.com"])
        self.assertEqual(self.read_store().find("old@example.com").expires_at, ny(2026, 3, 1))

        svc.bulk_set_expiry("gone@example.com", expires_at="2026-11-01", dry_run=False, root=self.root)
        self.assertEqual(self.read_store().find("gone@example.com").status, "cancelled")


class BulkArgumentErrorTests(BulkTestCase):
    def test_argument_errors(self):
        cases = [
            ("invalid_arguments", svc.bulk_add, ("a@example.com",), {}),  # no expiry rule
            ("invalid_arguments", svc.bulk_add, ("  # nothing",), {"months": 1}),  # no emails
            ("invalid_arguments", svc.bulk_extend, (), {"months": 1}),
            ("invalid_arguments", svc.bulk_extend, ("a@example.com",), {"months": 1, "all_active": True}),
            ("invalid_months", svc.bulk_extend, ("a@example.com",), {"months": 0}),
            ("invalid_expiry", svc.bulk_set_expiry, ("a@example.com",), {"expires_at": "null"}),
            ("invalid_arguments", svc.bulk_set_expiry, (), {"expires_at": "2026-11-01"}),
        ]
        for code, fn, args, kwargs in cases:
            with self.subTest(code=code, fn=fn.__name__, args=args, kwargs=kwargs):
                self.assert_code(code, fn, *args, root=self.root, **kwargs)


class PaymentTests(BulkTestCase):
    def test_validation(self):
        self.write_subscribers()
        bad = [
            {"currency": "CNY"},  # amount missing
            {"amount": "abc"},
            {"amount": -5},
            {"amount": 0},
            {"amount": True},
            {"amount": float("nan")},
            {"amount": 10, "bogus": 1},
            {"amount": 10, "currency": 5},
            {"amount": 10, "paid_at": "yesterday"},
        ]
        for payment in bad:
            with self.subTest(payment=payment), self.assertRaises(ServiceError) as ctx:
                svc.add_member("new@example.com", 1, payment=payment, root=self.root)
            self.assertIn(ctx.exception.code, ("invalid_payment", "invalid_date"))
        self.assertFalse(self.store_path().exists())

    def test_add_with_payment_round_trips_and_defaults(self):
        self.write_subscribers()
        svc.add_member(
            "new@example.com", 1, payment={"amount": "99.5", "currency": "cny", "channel": "Alipay", "paid_at": "2026-10-06", "ref": "T123"}, root=self.root
        )
        stored = self.read_store().find("new@example.com").history[-1]["payment"]
        self.assertEqual(stored, {"amount": 99.5, "currency": "CNY", "channel": "alipay", "paid_at": ny(2026, 10, 6).isoformat(), "ref": "T123"})
        self.assertEqual(json.loads(self.store_path().read_text(encoding="utf-8"))["version"], 1)

        svc.add_member("bare@example.com", 1, payment={"amount": 10}, root=self.root)
        stored = self.read_store().find("bare@example.com").history[-1]["payment"]
        self.assertEqual(stored, {"amount": 10, "currency": None, "channel": None, "paid_at": NOW.isoformat(), "ref": None})

    def test_record_payment_does_not_change_expiry_status_or_recipients(self):
        self.write_subscribers("a@example.com")
        member = self.active("a@example.com", months_total=2, note="orig")
        self.write_store([member, Member(email="gone@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 2, 9))])
        recipients_before = self.subscriber_file.read_bytes()
        result = svc.record_payment("a@example.com", 88, currency="CNY", channel="wechat", ref="R1", note="oct", root=self.root)
        after = self.read_store().find("a@example.com")
        self.assertEqual((after.expires_at, after.status, after.months_total, after.note), (ALIGNED, "active", 2, "orig"))
        self.assertEqual(after.history[-1]["action"], "payment")
        self.assertEqual(after.history[-1]["reason"], "oct")
        self.assertEqual(self.subscriber_file.read_bytes(), recipients_before)
        self.assertEqual(result["payment"]["amount"], 88)
        # also fine for a cancelled member, and still cancelled afterwards
        svc.record_payment("gone@example.com", 5, root=self.root)
        self.assertEqual(self.read_store().find("gone@example.com").status, "cancelled")

    def test_get_member_derives_payments_and_total_paid(self):
        self.write_subscribers()
        self.write_store([self.active("a@example.com"), self.active("fresh@example.com")])
        svc.record_payment("a@example.com", 10, currency="CNY", root=self.root)
        svc.record_payment("a@example.com", 0.1, currency="CNY", root=self.root)
        svc.record_payment("a@example.com", 0.2, currency="CNY", root=self.root)
        svc.record_payment("a@example.com", 7, root=self.root)
        svc.extend_member("a@example.com", 1, payment={"amount": 3, "currency": "USD"}, root=self.root)
        member = svc.get_member("a@example.com", root=self.root)["member"]
        self.assertEqual(len(member["payments"]), 5)
        self.assertEqual(member["total_paid"], {"CNY": 10.3, "USD": 3, "unspecified": 7})  # Decimal summation: no 10.300000000000001
        self.assertEqual((member["payments"][-1]["action"], member["payments"][-1]["amount"]), ("extend", 3))

        fresh = svc.get_member("fresh@example.com", root=self.root)["member"]
        self.assertEqual((fresh["payments"], fresh["total_paid"]), ([], {}))

    def test_payments_report_totals_filters_and_list(self):
        self.write_store([])
        self.assertEqual(svc.payments_report(root=self.root)["count"], 0)

        self.write_subscribers()
        self.write_store([self.active("a@example.com"), self.active("b@example.com")])
        svc.record_payment("a@example.com", 100, currency="CNY", channel="wechat", paid_at="2026-09-30", ref="r1", root=self.root)
        svc.record_payment("b@example.com", 50, currency="CNY", channel="alipay", paid_at="2026-10-02", root=self.root)
        svc.record_payment("b@example.com", 20, currency="USD", channel="wechat", paid_at="2026-10-05T10:00:00-04:00", root=self.root)
        svc.record_payment("a@example.com", 1, paid_at="2026-10-06", root=self.root)

        full = svc.payments_report(root=self.root)
        self.assertEqual(full["count"], 4)
        self.assertEqual(full["totals_by_currency"], {"CNY": 150, "USD": 20, "unspecified": 1})
        self.assertEqual(full["totals_by_channel"], {"alipay": {"CNY": 50}, "unspecified": {"unspecified": 1}, "wechat": {"CNY": 100, "USD": 20}})
        self.assertEqual([p["paid_at"][:10] for p in full["payments"]], ["2026-09-30", "2026-10-02", "2026-10-05", "2026-10-06"])
        self.assertEqual(set(full["payments"][0]), {"email", "paid_at", "amount", "currency", "channel", "ref", "action"})
        self.assertEqual((full["payments"][0]["ref"], full["payments"][0]["action"]), ("r1", "payment"))

        window = svc.payments_report(since="2026-10-01", until="2026-10-05", root=self.root)  # bare until date includes the whole day
        self.assertEqual(window["count"], 2)
        self.assertEqual(window["totals_by_currency"], {"CNY": 50, "USD": 20})


if __name__ == "__main__":
    unittest.main()
