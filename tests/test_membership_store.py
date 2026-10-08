import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from quantcheck.membership_store import (
    Member,
    MembershipStore,
    MembershipStoreError,
    default_path,
    load_or_create,
    load_store,
    save_store,
)

NY_OFFSET = "-04:00"  # America/New_York in EDT (used for readable fixture strings)


class DefaultPathTests(unittest.TestCase):
    def test_default_path_is_under_state(self):
        root = Path("/opt/quantcheck")
        self.assertEqual(default_path(root), root / "state" / "memberships.json")


class LoadStoreFailureTests(unittest.TestCase):
    """These failure modes are exactly what notify_routes must catch and
    fail OPEN on. See tests/test_notify_routes_membership.py for that half.
    """

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_corrupt_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_non_object_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_members_not_a_list_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps({"version": 1, "members": "oops"}), encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_member_missing_email_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps({"version": 1, "members": [{"joined_at": "2026-08-01T00:00:00-04:00"}]}), encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_member_missing_joined_at_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps({"version": 1, "members": [{"email": "a@example.com"}]}), encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_naive_joined_at_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(
                json.dumps({"version": 1, "members": [{"email": "a@example.com", "joined_at": "2026-08-01T00:00:00"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_non_dict_member_entry_raises_membership_store_error_not_a_raw_exception(self):
        # A malformed row (bare string/number/null/list instead of an
        # object) must surface as MembershipStoreError like every other
        # malformed-store case, not as an uncaught AttributeError/TypeError
        # -- callers on the fail-open path (notify_routes) only catch
        # MembershipStoreError, so anything else would crash the real send
        # pipeline instead of failing open.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps({"version": 1, "members": ["not-a-dict"]}), encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)

    def test_null_member_entry_raises_membership_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps({"version": 1, "members": [None]}), encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_store(path)


class LoadOrCreateTests(unittest.TestCase):
    def test_missing_file_returns_empty_store_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            store = load_or_create(path)
            self.assertEqual(store.members, [])
            self.assertEqual(store.path, path)

    def test_corrupt_file_still_raises(self):
        # Missing is benign (bootstrap); corrupt is never silently discarded.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(MembershipStoreError):
                load_or_create(path)


class RoundTripTests(unittest.TestCase):
    def test_save_then_load_preserves_all_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "memberships.json"  # parent dir does not exist yet
            store = MembershipStore(path=path, members=[])
            member = Member(
                email="x@y.com",
                status="active",
                joined_at=datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc),
                expires_at=datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc),
                months_total=1,
                note="migrated from notify_recipients.txt on 2026-08-31",
                history=[{"at": "2026-08-31T00:00:00+00:00", "action": "migrate", "months": None,
                          "expires_at": "2026-10-09T00:00:00+00:00", "actor": "migration"}],
            )
            store.upsert(member)
            save_store(store)

            self.assertTrue(path.exists())
            reloaded = load_store(path)

            self.assertEqual(len(reloaded.members), 1)
            got = reloaded.members[0]
            self.assertEqual(got.email, "x@y.com")
            self.assertEqual(got.status, "active")
            self.assertEqual(got.joined_at, member.joined_at)
            self.assertEqual(got.expires_at, member.expires_at)
            self.assertEqual(got.months_total, 1)
            self.assertEqual(got.note, member.note)
            self.assertEqual(got.history, member.history)

    def test_email_is_normalized_to_lowercase_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(
                json.dumps({"version": 1, "members": [{"email": "  Mixed.Case@Example.COM ", "joined_at": "2026-08-01T00:00:00-04:00"}]}),
                encoding="utf-8",
            )
            store = load_store(path)
            self.assertEqual(store.members[0].email, "mixed.case@example.com")


class AtomicWriteAndBackupTests(unittest.TestCase):
    def test_no_leftover_tmp_files_after_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            store = MembershipStore(path=path, members=[])
            save_store(store)
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_second_save_creates_timestamped_backup_of_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            store = MembershipStore(path=path, members=[])
            save_store(store)
            first_contents = path.read_text(encoding="utf-8")

            member = Member(
                email="a@example.com",
                status="active",
                joined_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
                expires_at=None,
            )
            store.upsert(member)
            backup_path = save_store(store)

            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.exists())
            self.assertEqual(backup_path.read_text(encoding="utf-8"), first_contents)
            self.assertIn("a@example.com", path.read_text(encoding="utf-8"))

    def test_backup_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            store = MembershipStore(path=path, members=[])
            save_store(store)
            backup_path = save_store(store, backup=False)
            self.assertIsNone(backup_path)
            self.assertEqual(list(Path(tmp).glob("*.bak")), [])


class MemberEffectiveStatusTests(unittest.TestCase):
    def test_active_when_before_expiry(self):
        member = Member(
            email="a@example.com", status="active",
            joined_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            expires_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(member.effective_status(now), "active")
        self.assertTrue(member.is_effectively_active(now))

    def test_expired_when_stored_status_is_stale_active(self):
        # Stored status says "active" but expires_at is in the past; the
        # derived status must win.
        member = Member(
            email="a@example.com", status="active",
            joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2026, 2, 9, tzinfo=timezone.utc),
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(member.effective_status(now), "expired")
        self.assertFalse(member.is_effectively_active(now))

    def test_legacy_never_expires(self):
        member = Member(
            email="a@example.com", status="legacy",
            joined_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            expires_at=None,
        )
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(member.effective_status(far_future), "legacy")
        self.assertTrue(member.is_effectively_active(far_future))

    def test_cancelled_is_sticky_even_if_expiry_is_in_the_future(self):
        member = Member(
            email="a@example.com", status="cancelled",
            joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),  # far future, would be "active" otherwise
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(member.effective_status(now), "cancelled")
        self.assertFalse(member.is_effectively_active(now))


class MembershipStoreCrudTests(unittest.TestCase):
    def test_find_is_case_insensitive(self):
        store = MembershipStore(path=Path("unused"), members=[])
        store.upsert(Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), expires_at=None))
        self.assertIsNotNone(store.find("A@EXAMPLE.COM"))
        self.assertIsNone(store.find("nobody@example.com"))

    def test_upsert_replaces_existing_member_by_email_instead_of_duplicating(self):
        # This is the core idempotency guarantee that migrate_from_recipients
        # depends on: re-running it must not create duplicate member rows.
        store = MembershipStore(path=Path("unused"), members=[])
        store.upsert(Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), expires_at=None, months_total=1))
        store.upsert(Member(email="A@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), expires_at=None, months_total=2))
        self.assertEqual(len(store.members), 1)
        self.assertEqual(store.members[0].months_total, 2)

    def test_add_history_appends_entry_with_expected_shape(self):
        member = Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), expires_at=None)
        member.add_history(action="add", months=1, expires_at=datetime(2026, 2, 9, tzinfo=timezone.utc), actor="admin_cli", at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(member.history), 1)
        entry = member.history[0]
        self.assertEqual(entry["action"], "add")
        self.assertEqual(entry["months"], 1)
        self.assertEqual(entry["actor"], "admin_cli")
        self.assertEqual(entry["expires_at"], "2026-02-09T00:00:00+00:00")
        self.assertEqual(entry["at"], "2026-01-01T00:00:00+00:00")



# A store exactly as written by the code that predates payments / expiry
# modes: no `payment` or `mode` keys anywhere in history.
OLD_FORMAT_STORE = {
    "version": 1,
    "timezone": "America/New_York",
    "members": [
        {
            "email": "old@example.com",
            "status": "active",
            "joined_at": "2026-08-31T08:38:54.315975-04:00",
            "expires_at": "2026-11-01T00:00:00-04:00",
            "months_total": 0,
            "note": "migrated from notify_recipients.txt on 2026-08-31",
            "history": [
                {"at": "2026-08-31T08:38:54.315975-04:00", "action": "migrate", "months": None, "expires_at": "2026-11-01T00:00:00-04:00", "actor": "migration", "reason": None}
            ],
        }
    ],
}


class PaymentFieldCompatibilityTests(unittest.TestCase):
    def test_store_written_by_old_code_still_loads_and_services_read_it(self):
        from quantcheck.service import members as svc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            (root / ".env").write_text(f"QUANTCHECK_HOME={root}\n", encoding="utf-8")
            path = default_path(root)
            path.write_text(json.dumps(OLD_FORMAT_STORE), encoding="utf-8")

            store = load_store(path)
            self.assertEqual(store.members[0].history[0]["action"], "migrate")

            detail = svc.get_member("old@example.com", root=root)["member"]
            self.assertEqual((detail["payments"], detail["total_paid"]), ([], {}))
            self.assertEqual(svc.payments_report(root=root)["count"], 0)

    def test_new_history_keys_are_additive_and_do_not_change_top_level_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memberships.json"
            path.write_text(json.dumps(OLD_FORMAT_STORE), encoding="utf-8")
            store = load_store(path)
            member = store.members[0]
            member.add_history(
                action="payment", months=None, expires_at=member.expires_at, actor="t", extra={"payment": {"amount": 5}, "mode": "x", "action": "ignored"}
            )
            save_store(store, backup=False)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(raw), {"version", "timezone", "members"})
            self.assertEqual(set(raw["members"][0]), set(OLD_FORMAT_STORE["members"][0]))
            entry = raw["members"][0]["history"][-1]
            self.assertEqual(entry["action"], "payment")  # extra can never overwrite core keys
            self.assertEqual(entry["payment"], {"amount": 5})
            self.assertEqual(load_store(path).members[0].history[-1]["mode"], "x")


if __name__ == "__main__":
    unittest.main()
