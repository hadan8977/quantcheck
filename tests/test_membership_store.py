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

UTC = timezone.utc


def member(email="a@example.com", status="active", expires_at=None, **kwargs):
    return Member(email=email, status=status, joined_at=datetime(2026, 1, 1, tzinfo=UTC), expires_at=expires_at, **kwargs)


class LoadStoreTests(unittest.TestCase):
    """The failure modes here are exactly what notify_routes must catch and
    fail OPEN on. See tests/test_notify_routes_membership.py for that half.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "memberships.json"

    def test_default_path_is_under_state(self):
        root = Path("/opt/quantcheck")
        self.assertEqual(default_path(root), root / "state" / "memberships.json")

    def test_malformed_stores_raise_membership_store_error(self):
        # A non-dict member entry must surface as MembershipStoreError, not a raw
        # AttributeError/TypeError: callers on the fail-open path (notify_routes)
        # only catch MembershipStoreError, so anything else would crash the send.
        bad_joined = {"email": "a@example.com", "joined_at": "2026-08-01T00:00:00"}  # naive
        cases = {
            "corrupt json": "{not valid json",
            "non-object json": "[1, 2, 3]",
            "members not a list": json.dumps({"version": 1, "members": "oops"}),
            "member missing email": json.dumps({"version": 1, "members": [{"joined_at": "2026-08-01T00:00:00-04:00"}]}),
            "member missing joined_at": json.dumps({"version": 1, "members": [{"email": "a@example.com"}]}),
            "naive joined_at": json.dumps({"version": 1, "members": [bad_joined]}),
            "bare string entry": json.dumps({"version": 1, "members": ["not-a-dict"]}),
            "null entry": json.dumps({"version": 1, "members": [None]}),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(MembershipStoreError):
                    load_store(self.path)
                if name == "corrupt json":
                    # Missing is benign (bootstrap); corrupt is never silently discarded.
                    with self.assertRaises(MembershipStoreError):
                        load_or_create(self.path)

    def test_missing_file_raises_in_load_store_but_load_or_create_returns_empty_store(self):
        with self.assertRaises(MembershipStoreError):
            load_store(self.path)
        store = load_or_create(self.path)
        self.assertEqual(store.members, [])
        self.assertEqual(store.path, self.path)

    def test_email_is_normalized_to_lowercase_on_load(self):
        self.path.write_text(
            json.dumps({"version": 1, "members": [{"email": "  Mixed.Case@Example.COM ", "joined_at": "2026-08-01T00:00:00-04:00"}]}),
            encoding="utf-8",
        )
        self.assertEqual(load_store(self.path).members[0].email, "mixed.case@example.com")


class SaveStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_save_then_load_preserves_all_fields(self):
        path = self.dir / "state" / "memberships.json"  # parent dir does not exist yet
        store = MembershipStore(path=path, members=[])
        original = Member(
            email="x@y.com",
            status="active",
            joined_at=datetime(2026, 8, 31, tzinfo=UTC),
            expires_at=datetime(2026, 10, 9, tzinfo=UTC),
            months_total=1,
            note="migrated from notify_recipients.txt on 2026-08-31",
            history=[{"at": "2026-08-31T00:00:00+00:00", "action": "migrate", "months": None,
                      "expires_at": "2026-10-09T00:00:00+00:00", "actor": "migration"}],
        )
        store.upsert(original)
        save_store(store)

        reloaded = load_store(path)

        self.assertEqual(len(reloaded.members), 1)
        got = reloaded.members[0]
        for attr in ("email", "status", "joined_at", "expires_at", "months_total", "note", "history"):
            with self.subTest(attr):
                self.assertEqual(getattr(got, attr), getattr(original, attr))

    def test_atomic_write_backup_and_backup_opt_out(self):
        path = self.dir / "memberships.json"
        store = MembershipStore(path=path, members=[])
        self.assertIsNone(save_store(store))  # nothing to back up the first time
        first_contents = path.read_text(encoding="utf-8")

        store.upsert(member())
        backup_path = save_store(store)

        self.assertIsNotNone(backup_path)
        self.assertEqual(backup_path.read_text(encoding="utf-8"), first_contents)
        self.assertIn("a@example.com", path.read_text(encoding="utf-8"))

        before = set(self.dir.glob("*.bak"))
        self.assertIsNone(save_store(store, backup=False))
        self.assertEqual(set(self.dir.glob("*.bak")), before)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])  # atomic: no leftover tmp files


class MemberTests(unittest.TestCase):
    def test_effective_status(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)
        cases = {
            "active before expiry": (member(expires_at=datetime(2026, 10, 9, tzinfo=UTC)), "active", True),
            # Stored "active" but expires_at in the past: the derived status wins.
            "stale stored active is expired": (member(expires_at=datetime(2026, 2, 9, tzinfo=UTC)), "expired", False),
            "legacy never expires": (member(status="legacy"), "legacy", True, datetime(2099, 1, 1, tzinfo=UTC)),
            # cancelled is sticky even with a far-future expiry.
            "cancelled is sticky": (member(status="cancelled", expires_at=datetime(2099, 1, 1, tzinfo=UTC)), "cancelled", False),
        }
        for name, (m, status, active, *at) in cases.items():
            with self.subTest(name):
                self.assertEqual(m.effective_status(*at or [now]), status)
                self.assertIs(m.is_effectively_active(*at or [now]), active)

    def test_store_find_upsert_and_add_history(self):
        store = MembershipStore(path=Path("unused"), members=[])
        store.upsert(member(months_total=1))
        self.assertIsNotNone(store.find("A@EXAMPLE.COM"))  # case-insensitive
        self.assertIsNone(store.find("nobody@example.com"))
        # upsert replaces by email: the idempotency migrate_from_recipients relies on.
        store.upsert(member(email="A@example.com", months_total=2))
        self.assertEqual(len(store.members), 1)
        self.assertEqual(store.members[0].months_total, 2)

        m = store.members[0]
        m.add_history(action="add", months=1, expires_at=datetime(2026, 2, 9, tzinfo=UTC), actor="admin_cli", at=datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(len(m.history), 1)
        self.assertEqual(
            {k: m.history[0][k] for k in ("action", "months", "actor", "expires_at", "at")},
            {"action": "add", "months": 1, "actor": "admin_cli",
             "expires_at": "2026-02-09T00:00:00+00:00", "at": "2026-01-01T00:00:00+00:00"},
        )


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
