import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quantcheck.membership import MEMBERSHIP_TZ
from quantcheck.membership_store import Member, MembershipStore, save_store
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
        from quantcheck.membership_store import load_store

        return load_store(self.store_path())


class ListAndGetMembersTests(ServiceMembersTestCase):
    def test_list_members_empty_store_returns_empty_not_an_error(self):
        result = svc.list_members(root=self.root)
        self.assertEqual(result, {"count": 0, "members": []})

    def test_list_members_filters_by_status(self):
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1)),
                Member(email="expired@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1)),
            ]
        )
        result = svc.list_members(status="expired", root=self.root)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["members"][0]["email"], "expired@example.com")

    def test_list_members_rejects_invalid_status(self):
        with self.assertRaises(ServiceError) as ctx:
            svc.list_members(status="bogus", root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_status")

    def test_list_members_expiring_within_days(self):
        now = datetime.now(MEMBERSHIP_TZ)
        self.write_store(
            [
                Member(email="soon@example.com", status="active", joined_at=now - timedelta(days=10), expires_at=now + timedelta(days=5)),
                Member(email="later@example.com", status="active", joined_at=now - timedelta(days=10), expires_at=now + timedelta(days=90)),
            ]
        )
        result = svc.list_members(expiring_within_days=14, root=self.root)
        self.assertEqual([m["email"] for m in result["members"]], ["soon@example.com"])

    def test_get_member_not_found_raises_service_error(self):
        self.write_store([])
        with self.assertRaises(ServiceError) as ctx:
            svc.get_member("nobody@example.com", root=self.root)
        self.assertEqual(ctx.exception.code, "member_not_found")

    def test_get_member_returns_history(self):
        member = Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))
        member.add_history(action="add", months=1, expires_at=ny(2099, 1, 1), actor="test", at=ny(2026, 1, 1))
        self.write_store([member])
        result = svc.get_member("A@Example.com", root=self.root)
        self.assertEqual(result["member"]["email"], "a@example.com")
        self.assertEqual(len(result["member"]["history"]), 1)

    def test_corrupt_store_raises_service_error_not_fail_open(self):
        # Unlike notify_routes.subscriber_recipients, admin-facing reads
        # must surface storage corruption loudly, not silently paper over it.
        self.store_path().parent.mkdir(parents=True, exist_ok=True)
        self.store_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(ServiceError) as ctx:
            svc.list_members(root=self.root)
        self.assertEqual(ctx.exception.code, "membership_store_error")


class AddMemberTests(ServiceMembersTestCase):
    def test_add_member_computes_expiry_and_creates_store(self):
        self.write_subscribers()
        result = svc.add_member("new@example.com", 1, note="test add", joined_at="2026-08-10", root=self.root)

        self.assertEqual(result["member"]["email"], "new@example.com")
        self.assertEqual(result["member"]["expires_at"], ny(2026, 9, 9).isoformat())
        self.assertEqual(result["member"]["status"], "active")
        self.assertEqual(result["member"]["months_total"], 1)

    def test_add_member_appends_to_recipients_file(self):
        self.write_subscribers("existing@example.com")
        svc.add_member("new@example.com", 1, joined_at="2026-08-10", root=self.root)
        contents = self.subscriber_file.read_text(encoding="utf-8")
        self.assertIn("existing@example.com", contents)
        self.assertIn("new@example.com", contents)

    def test_add_member_creates_backup_of_recipients_file_when_it_already_existed(self):
        self.write_subscribers("existing@example.com")
        svc.add_member("new@example.com", 1, joined_at="2026-08-10", root=self.root)
        backups = list(self.root.glob("notify_recipients.txt.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_add_member_rejects_invalid_email(self):
        with self.assertRaises(ServiceError) as ctx:
            svc.add_member("not-an-email", 1, root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_email")

    def test_add_member_rejects_duplicate(self):
        svc.add_member("dup@example.com", 1, joined_at="2026-08-10", root=self.root)
        with self.assertRaises(ServiceError) as ctx:
            svc.add_member("DUP@example.com", 1, joined_at="2026-08-10", root=self.root)
        self.assertEqual(ctx.exception.code, "member_already_exists")

    def test_add_member_writes_history_entry(self):
        svc.add_member("new@example.com", 3, note="paid 3mo", joined_at="2026-08-10", root=self.root)
        store = self.read_store()
        member = store.find("new@example.com")
        self.assertEqual(len(member.history), 1)
        self.assertEqual(member.history[0]["action"], "add")
        self.assertEqual(member.history[0]["months"], 3)


class ExtendMemberTests(ServiceMembersTestCase):
    def test_extend_stacks_on_existing_expiry(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 8, 10), expires_at=ny(2026, 9, 9))])
        result = svc.extend_member("a@example.com", 3, root=self.root)
        self.assertEqual(result["member"]["expires_at"], ny(2026, 12, 9).isoformat())
        self.assertEqual(result["member"]["months_total"], 3)

    def test_extend_not_found_raises(self):
        self.write_store([])
        with self.assertRaises(ServiceError) as ctx:
            svc.extend_member("nobody@example.com", 1, root=self.root)
        self.assertEqual(ctx.exception.code, "member_not_found")

    def test_extend_reinstates_a_cancelled_member(self):
        self.write_store([Member(email="a@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1))])
        result = svc.extend_member("a@example.com", 1, root=self.root)
        self.assertEqual(result["member"]["stored_status"], "active")

    def test_extend_re_adds_to_recipients_file_if_previously_removed(self):
        self.write_subscribers()  # empty: as if remove_member had taken them out
        self.write_store([Member(email="a@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1))])
        svc.extend_member("a@example.com", 1, root=self.root)
        self.assertIn("a@example.com", self.subscriber_file.read_text(encoding="utf-8"))

    def test_months_total_accumulates_across_multiple_extends(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1), months_total=1)])
        svc.extend_member("a@example.com", 2, root=self.root)
        svc.extend_member("a@example.com", 3, root=self.root)
        store = self.read_store()
        self.assertEqual(store.find("a@example.com").months_total, 6)


class SetExpiryTests(ServiceMembersTestCase):
    def test_set_expiry_to_explicit_date(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 9, 9))])
        result = svc.set_expiry("a@example.com", "2026-12-09", "manual correction", root=self.root)
        self.assertEqual(result["member"]["expires_at"], ny(2026, 12, 9).isoformat())

    def test_set_expiry_to_null_makes_legacy(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 9, 9))])
        result = svc.set_expiry("a@example.com", None, "grandfathered", root=self.root)
        self.assertIsNone(result["member"]["expires_at"])
        self.assertEqual(result["member"]["status"], "legacy")

    def test_set_expiry_does_not_reactivate_cancelled_status(self):
        # set_expiry is a pure date-correction tool; it must not have the
        # side effect of un-cancelling someone. Use extend_member for that.
        self.write_store([Member(email="a@example.com", status="cancelled", joined_at=ny(2026, 1, 1), expires_at=ny(2020, 1, 1))])
        result = svc.set_expiry("a@example.com", "2099-01-01", "typo fix", root=self.root)
        self.assertEqual(result["member"]["stored_status"], "cancelled")
        self.assertEqual(result["member"]["status"], "cancelled")

    def test_set_expiry_records_reason_in_history(self):
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2026, 9, 9))])
        svc.set_expiry("a@example.com", "2026-12-09", "fixed typo from 09-09", root=self.root)
        store = self.read_store()
        entry = store.find("a@example.com").history[-1]
        self.assertEqual(entry["reason"], "fixed typo from 09-09")

    def test_set_expiry_not_found_raises(self):
        self.write_store([])
        with self.assertRaises(ServiceError):
            svc.set_expiry("nobody@example.com", "2026-12-09", "n/a", root=self.root)


class RemoveMemberTests(ServiceMembersTestCase):
    def test_remove_marks_cancelled_and_removes_from_file(self):
        self.write_subscribers("a@example.com", "b@example.com")
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))])

        result = svc.remove_member("a@example.com", "refund requested", root=self.root)

        self.assertEqual(result["member"]["stored_status"], "cancelled")
        self.assertEqual(result["member"]["status"], "cancelled")
        contents = self.subscriber_file.read_text(encoding="utf-8")
        self.assertNotIn("a@example.com", contents)
        self.assertIn("b@example.com", contents)

    def test_remove_preserves_history(self):
        self.write_subscribers("a@example.com")
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))])
        svc.remove_member("a@example.com", "chargeback", root=self.root)
        store = self.read_store()
        member = store.find("a@example.com")
        self.assertEqual(member.history[-1]["action"], "remove")
        self.assertEqual(member.history[-1]["reason"], "chargeback")
        # Removed but not forgotten: the record itself must survive.
        self.assertIsNotNone(member)

    def test_remove_not_found_raises(self):
        self.write_store([])
        with self.assertRaises(ServiceError) as ctx:
            svc.remove_member("nobody@example.com", "n/a", root=self.root)
        self.assertEqual(ctx.exception.code, "member_not_found")


class ExpiringReportTests(ServiceMembersTestCase):
    def test_counts_by_bucket(self):
        now = datetime.now(MEMBERSHIP_TZ)
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=now - timedelta(days=100), expires_at=now + timedelta(days=100)),
                Member(email="soon@example.com", status="active", joined_at=now - timedelta(days=100), expires_at=now + timedelta(days=3)),
                Member(email="expired@example.com", status="active", joined_at=now - timedelta(days=100), expires_at=now - timedelta(days=1)),
                Member(email="cancelled@example.com", status="cancelled", joined_at=now - timedelta(days=100), expires_at=now + timedelta(days=100)),
                Member(email="legacy@example.com", status="legacy", joined_at=now - timedelta(days=100), expires_at=None),
            ]
        )
        report = svc.expiring_report(within_days=14, root=self.root)
        self.assertEqual(report["expired_count"], 1)
        self.assertEqual(report["cancelled_count"], 1)
        self.assertEqual(report["legacy_count"], 1)
        self.assertEqual(report["expiring_count"], 1)
        self.assertEqual(report["expiring"][0]["email"], "soon@example.com")
        # "active" includes soon-to-expire (expiring is a subset of active).
        self.assertEqual(report["active_count"], 2)


class SyncRecipientsTests(ServiceMembersTestCase):
    def test_reports_drift_without_modifying_anything(self):
        self.write_subscribers("in_file_only@example.com", "active@example.com")
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1)),
                Member(email="missing_from_file@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1)),
            ]
        )
        before_file = self.subscriber_file.read_text(encoding="utf-8")
        before_store = self.store_path().read_text(encoding="utf-8")

        result = svc.sync_recipients(root=self.root)

        self.assertEqual(result["drift"]["in_file_unknown_to_store"], ["in_file_only@example.com"])
        self.assertEqual(result["drift"]["active_members_missing_from_file"], ["missing_from_file@example.com"])
        self.assertFalse(result["in_sync"])
        # Read-only: files must be byte-identical after the call.
        self.assertEqual(self.subscriber_file.read_text(encoding="utf-8"), before_file)
        self.assertEqual(self.store_path().read_text(encoding="utf-8"), before_store)

    def test_in_sync_when_matching(self):
        self.write_subscribers("a@example.com")
        self.write_store([Member(email="a@example.com", status="active", joined_at=ny(2026, 1, 1), expires_at=ny(2099, 1, 1))])
        result = svc.sync_recipients(root=self.root)
        self.assertTrue(result["in_sync"])


class MigrateFromRecipientsTests(ServiceMembersTestCase):
    def test_dry_run_reports_plan_without_writing(self):
        self.write_subscribers("a@example.com", "b@example.com")
        result = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=True, root=self.root)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["to_migrate_count"], 2)
        self.assertEqual(sorted(result["to_migrate"]), ["a@example.com", "b@example.com"])
        self.assertFalse(self.store_path().exists())

    def test_execute_creates_members_with_expected_shape(self):
        self.write_subscribers("a@example.com", "b@example.com")
        result = svc.migrate_from_recipients("2026-10-09", "migrated from notify_recipients.txt on 2026-08-31", dry_run=False, root=self.root)

        self.assertEqual(result["migrated_count"], 2)
        store = self.read_store()
        self.assertEqual(len(store.members), 2)
        member = store.find("a@example.com")
        self.assertEqual(member.expires_at, ny(2026, 10, 9))
        self.assertEqual(member.status, "active")
        self.assertEqual(member.note, "migrated from notify_recipients.txt on 2026-08-31")
        self.assertEqual(member.history[0]["action"], "migrate")
        self.assertEqual(member.history[0]["actor"], "migration")

    def test_execute_backs_up_recipients_file(self):
        self.write_subscribers("a@example.com")
        svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=False, root=self.root)
        backups = list(self.root.glob("notify_recipients.txt.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_idempotent_rerun_does_not_duplicate_or_overwrite(self):
        self.write_subscribers("a@example.com", "b@example.com")
        first = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=False, root=self.root)
        self.assertEqual(first["migrated_count"], 2)

        # Simulate a manual correction to one member's expiry in between runs.
        svc.set_expiry("a@example.com", "2026-11-09", "actually paid through November", root=self.root)

        second = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=False, root=self.root)
        self.assertEqual(second["migrated_count"], 0)
        self.assertEqual(second["already_migrated_count"], 2)

        store = self.read_store()
        self.assertEqual(len(store.members), 2)  # no duplicates
        # The manual correction must survive a re-run untouched.
        self.assertEqual(store.find("a@example.com").expires_at, ny(2026, 11, 9))
        self.assertEqual(store.find("b@example.com").expires_at, ny(2026, 10, 9))

    def test_rejects_null_expiry(self):
        self.write_subscribers("a@example.com")
        with self.assertRaises(ServiceError) as ctx:
            svc.migrate_from_recipients(None, "migrated", dry_run=True, root=self.root)
        self.assertEqual(ctx.exception.code, "invalid_expiry")

    def test_81_subscriber_scale_migrates_cleanly(self):
        emails = [f"user{i}@example.com" for i in range(81)]
        self.write_subscribers(*emails)
        result = svc.migrate_from_recipients("2026-10-09", "migrated", dry_run=False, root=self.root)
        self.assertEqual(result["migrated_count"], 81)
        store = self.read_store()
        self.assertEqual(len(store.members), 81)
        for member in store.members:
            self.assertEqual(member.expires_at, ny(2026, 10, 9))
            self.assertEqual(member.status, "active")


if __name__ == "__main__":
    unittest.main()
