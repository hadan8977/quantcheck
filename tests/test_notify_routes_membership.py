import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck.membership_store import Member, MembershipStore, save_store
from quantcheck.notify_routes import EmailRoute, admin_recipients, recipients_for_route, route_preview, subscriber_recipients

UTC = timezone.utc


FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
PAST = datetime(2020, 1, 1, tzinfo=UTC)


def _member(email, *, status="active", expires_at=None, joined=datetime(2026, 1, 1, tzinfo=UTC)):
    return Member(email=email, status=status, joined_at=joined, expires_at=expires_at)


class MembershipFilterTestCase(unittest.TestCase):
    """Base fixture: tmp root with its own subscriber file, admin file, and
    membership store, and LOG_FILE patched to tmp so nothing touches the
    real repo's logs/ or state/ during tests (see B9 test-hygiene rule).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.subscriber_file = self.root / "notify_recipients.txt"
        self.admin_file = self.root / "notify_admin_recipients.txt"
        self.store_path = self.root / "state" / "memberships.json"
        self.log_file = self.root / "logs" / "notify_routes.log"

        self._log_patch = patch("quantcheck.notify_routes.LOG_FILE", self.log_file)
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)

    def base_env(self, **overrides) -> dict:
        env = {
            "NOTIFY_EMAIL_FILE": str(self.subscriber_file),
            "NOTIFY_EMAIL_TO": "",
            "NOTIFY_ADMIN_EMAIL_FILE": str(self.admin_file),
            "NOTIFY_ADMIN_EMAIL_TO": "",
            "MEMBERSHIP_STORE_FILE": str(self.store_path),
        }
        env.update(overrides)
        return env

    def write_subscribers(self, *emails):
        self.subscriber_file.write_text("\n".join(emails) + "\n", encoding="utf-8")

    def write_admins(self, *emails):
        self.admin_file.write_text("\n".join(emails) + "\n", encoding="utf-8")

    def write_store(self, members):
        store = MembershipStore(path=self.store_path, members=list(members))
        save_store(store, backup=False)

    def write_raw_store(self, text):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(text, encoding="utf-8")

    def read_log(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""


class EnforcementFilteringTests(MembershipFilterTestCase):
    def test_filtering_by_member_state(self):
        cases = {
            "active member is included": (_member("a@example.com", expires_at=FUTURE), ["a@example.com"]),
            "expired member is excluded": (_member("a@example.com", expires_at=PAST), []),
            "cancelled is excluded even with future expiry": (_member("a@example.com", status="cancelled", expires_at=FUTURE), []),
            "legacy never expires": (_member("a@example.com", status="legacy", expires_at=None), ["a@example.com"]),
        }
        for name, (member, expected) in cases.items():
            with self.subTest(name):
                self.write_subscribers("a@example.com")
                self.write_store([member])
                self.assertEqual(subscriber_recipients(self.base_env()), expected)

    def test_mixed_list_only_excludes_expired_and_payment_history_does_not_matter(self):
        # History entries carry free-form `payment`/`mode` keys; routing must be unaffected.
        self.write_subscribers("paid@example.com", "lapsed@example.com", "unknown@example.com")
        paid = _member("paid@example.com", expires_at=FUTURE)
        paid.add_history(action="add", months=None, expires_at=paid.expires_at, actor="t", extra={"mode": "align", "payment": {"amount": 9, "currency": "CNY"}})
        lapsed = _member("lapsed@example.com", expires_at=PAST)
        lapsed.add_history(action="payment", months=None, expires_at=lapsed.expires_at, actor="t", extra={"payment": {"amount": 1}})
        self.write_store([paid, lapsed])

        self.assertEqual(subscriber_recipients(self.base_env()), ["paid@example.com", "unknown@example.com"])

    def test_unknown_email_fails_open_and_logs_a_warning(self):
        # In notify_recipients.txt but absent from the store: valid, never silently dropped.
        self.write_subscribers("unknown@example.com")
        self.write_store([])  # store loads fine, just has nobody in it

        self.assertEqual(subscriber_recipients(self.base_env()), ["unknown@example.com"])

        log_contents = self.read_log()
        self.assertIn("warning", log_contents.lower())
        self.assertIn("1", log_contents)

    def test_every_call_logs_summary_and_lists_excluded_addresses(self):
        self.write_subscribers("active@example.com")
        self.write_store([_member("active@example.com", expires_at=FUTURE)])
        subscriber_recipients(self.base_env())
        log_contents = self.read_log()
        for expected in ("subscribers=1", "active=1", "excluded=0"):
            self.assertIn(expected, log_contents)

        self.write_subscribers("expired@example.com")
        self.write_store([_member("expired@example.com", expires_at=PAST)])
        subscriber_recipients(self.base_env())
        self.assertIn("expired@example.com", self.read_log())


class StoreMissingOrCorruptFailOpenTests(MembershipFilterTestCase):
    """The highest-priority safety requirement: a broken or missing membership
    store must never cause subscribers to silently stop receiving mail. Every
    case must return the full original list AND log loudly.
    """

    def test_broken_store_fails_open_with_full_list_and_logs_loudly(self):
        cases = {
            "missing file": None,
            "corrupt json": "{not valid json at all",
            # One bad row anywhere in the file must not take down the whole list.
            "member missing joined_at": json.dumps({"version": 1, "members": [{"email": "a@example.com"}]}),
            # Regression: a bare-string row used to raise an uncaught AttributeError and crash the send path.
            "non-dict member entry": json.dumps({"version": 1, "members": ["not-a-dict"]}),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.write_subscribers("a@example.com", "b@example.com", "c@example.com")
                self.store_path.unlink(missing_ok=True)
                self.log_file.unlink(missing_ok=True)
                if raw is not None:
                    self.write_raw_store(raw)

                self.assertEqual(subscriber_recipients(self.base_env()), ["a@example.com", "b@example.com", "c@example.com"])
                self.assertIn("UNAVAILABLE", self.read_log())


class KillSwitchTests(MembershipFilterTestCase):
    def test_enforcement_zero_returns_full_list_and_never_needs_the_store(self):
        self.write_subscribers("expired@example.com", "active@example.com")
        self.write_store([_member("expired@example.com", expires_at=PAST), _member("active@example.com", expires_at=FUTURE)])
        off = self.base_env(MEMBERSHIP_ENFORCEMENT="0")
        self.assertEqual(subscriber_recipients(off), ["expired@example.com", "active@example.com"])

        # The kill switch must work even if the store itself is the thing on fire.
        self.write_raw_store("{completely broken")
        self.assertEqual(subscriber_recipients(off), ["expired@example.com", "active@example.com"])

    def test_default_is_enforcement_on(self):
        self.write_subscribers("expired@example.com")
        self.write_store([_member("expired@example.com", expires_at=PAST)])

        env = self.base_env()
        env.pop("MEMBERSHIP_ENFORCEMENT", None)
        self.assertEqual(subscriber_recipients(env), [])


class AdminNeverFilteredTests(MembershipFilterTestCase):
    def test_admin_recipients_ignore_the_membership_store(self):
        self.write_admins("admin@example.com")
        # No membership store at all; touching it would raise or behave differently.
        self.assertEqual(admin_recipients(self.base_env()), ["admin@example.com"])

        # Even an admin address listed as an expired member is not filtered on the ADMIN route.
        self.write_store([_member("admin@example.com", expires_at=PAST)])
        self.assertEqual(recipients_for_route(EmailRoute.ADMIN, self.base_env()), ["admin@example.com"])

    def test_picks_update_route_filters_subscribers_but_keeps_admin(self):
        self.write_subscribers("expired@example.com")
        self.write_admins("admin@example.com")
        self.write_store([_member("expired@example.com", expires_at=PAST)])

        self.assertEqual(recipients_for_route(EmailRoute.PICKS_UPDATE, self.base_env()), ["admin@example.com"])


class RoutePreviewTests(MembershipFilterTestCase):
    def test_preview_reports_included_excluded_counts_and_is_silent(self):
        self.write_subscribers("active@example.com", "expired@example.com", "unknown@example.com")
        self.write_admins("admin@example.com")
        self.write_store([_member("active@example.com", expires_at=FUTURE), _member("expired@example.com", expires_at=PAST)])

        preview = route_preview(EmailRoute.PICKS_UPDATE, self.base_env())

        self.assertEqual(preview["enforcement"], True)
        self.assertEqual(set(preview["included"]), {"active@example.com", "unknown@example.com", "admin@example.com"})
        self.assertEqual([(e["email"], e["reason"]) for e in preview["excluded"]], [("expired@example.com", "expired")])
        self.assertEqual(preview["counts"]["excluded"], 1)
        self.assertEqual(preview["counts"]["unknown_fail_open"], 1)
        json.dumps(preview)  # must be JSON serializable
        # A read-only diagnostic must not spam notify_routes.log the way a real send does.
        self.assertEqual(self.read_log(), "")

    def test_preview_for_admin_route_ignores_subscribers_and_missing_store_fails_open(self):
        self.write_subscribers("expired@example.com")
        self.write_admins("admin@example.com")
        self.write_store([_member("expired@example.com", expires_at=PAST)])
        admin = route_preview(EmailRoute.ADMIN, self.base_env())
        self.assertEqual((admin["included"], admin["excluded"]), (["admin@example.com"], []))

        self.store_path.unlink()
        broken = route_preview(EmailRoute.PICKS_UPDATE, self.base_env())
        self.assertEqual(set(broken["included"]), {"expired@example.com", "admin@example.com"})
        self.assertEqual(broken["excluded"], [])
        self.assertIn("store_error", broken)


class ExplicitRootOverridesFrozenModuleRootTests(unittest.TestCase):
    """Regression: quantcheck.notify_routes.ROOT is computed once at import
    from the ambient QUANTCHECK_HOME. `quantcheck-admin --root <X>` needs
    relative NOTIFY_EMAIL_FILE / NOTIFY_ADMIN_EMAIL_FILE / MEMBERSHIP_STORE_FILE
    settings to resolve against <X>, not the frozen root (in production always
    /opt/quantcheck). Before the fix this silently read the real subscriber
    list while operating against a throwaway root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.intended_root = Path(self._tmp.name)
        (self.intended_root / "state").mkdir(parents=True)
        log_patch = patch("quantcheck.notify_routes.LOG_FILE", self.intended_root / "logs" / "notify_routes.log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_relative_files_resolve_against_explicit_root_not_frozen_root(self):
        (self.intended_root / "notify_recipients.txt").write_text("tmp-only@example.com\n", encoding="utf-8")
        (self.intended_root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        # Deliberately relative: the shape .env has in production.
        env = {"NOTIFY_EMAIL_FILE": "notify_recipients.txt", "NOTIFY_EMAIL_TO": "", "MEMBERSHIP_ENFORCEMENT": "0",
               "NOTIFY_ADMIN_EMAIL_FILE": "notify_admin_recipients.txt", "NOTIFY_ADMIN_EMAIL_TO": ""}

        with patch("quantcheck.notify_routes.ROOT", Path("/definitely/not/the/intended/root")):
            self.assertEqual(subscriber_recipients(env, root=self.intended_root), ["tmp-only@example.com"])
            self.assertEqual(admin_recipients(env, root=self.intended_root), ["admin@example.com"])

            # route_preview loads the store from the intended root too: empty store, so
            # the address is unknown-to-store (fail open), not a store_error.
            save_store(MembershipStore(path=self.intended_root / "state" / "memberships.json", members=[]), backup=False)
            preview = route_preview(EmailRoute.PICKS_UPDATE, {**env, "MEMBERSHIP_ENFORCEMENT": "1"}, root=self.intended_root)

        self.assertNotIn("store_error", preview)
        self.assertEqual(preview["counts"]["unknown_fail_open"], 1)
        self.assertEqual(set(preview["included"]), {"tmp-only@example.com", "admin@example.com"})

    def test_omitting_root_still_falls_back_to_module_root_unchanged(self):
        # Backward compatibility: callers that never pass root= see the same behavior as before.
        (self.intended_root / "notify_recipients.txt").write_text("fallback@example.com\n", encoding="utf-8")
        env = {"NOTIFY_EMAIL_FILE": "notify_recipients.txt", "NOTIFY_EMAIL_TO": "", "MEMBERSHIP_ENFORCEMENT": "0"}

        with patch("quantcheck.notify_routes.ROOT", self.intended_root):
            self.assertEqual(subscriber_recipients(env), ["fallback@example.com"])


if __name__ == "__main__":
    unittest.main()
