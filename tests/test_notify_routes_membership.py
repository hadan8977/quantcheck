import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck.membership_store import Member, MembershipStore, save_store
from quantcheck.notify_routes import EmailRoute, admin_recipients, recipients_for_route, route_preview, subscriber_recipients

UTC = timezone.utc


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

    def read_log(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""


class EnforcementOnFilteringTests(MembershipFilterTestCase):
    def test_active_member_is_included(self):
        self.write_subscribers("active@example.com")
        self.write_store([_member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC))])

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["active@example.com"])

    def test_expired_member_is_excluded(self):
        self.write_subscribers("expired@example.com")
        self.write_store([_member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, [])

    def test_cancelled_member_is_excluded_even_with_future_expiry(self):
        self.write_subscribers("cancelled@example.com")
        self.write_store([_member("cancelled@example.com", status="cancelled", expires_at=datetime(2099, 1, 1, tzinfo=UTC))])

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, [])

    def test_legacy_member_never_expires(self):
        self.write_subscribers("legacy@example.com")
        self.write_store([_member("legacy@example.com", status="legacy", expires_at=None)])

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["legacy@example.com"])

    def test_mixed_list_only_excludes_the_expired_ones(self):
        self.write_subscribers("active@example.com", "expired@example.com", "unknown@example.com")
        self.write_store([
            _member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC)),
            _member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
        ])

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["active@example.com", "unknown@example.com"])


class UnknownEmailFailOpenTests(MembershipFilterTestCase):
    """B3 requirement: an email present in notify_recipients.txt but absent
    from the membership store must be treated as valid (fail-open), with a
    warning logged -- never silently dropped.
    """

    def test_unknown_email_is_included(self):
        self.write_subscribers("unknown@example.com")
        self.write_store([])  # store loads fine, just has nobody in it

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["unknown@example.com"])

    def test_unknown_email_logs_a_warning(self):
        self.write_subscribers("unknown@example.com")
        self.write_store([])

        subscriber_recipients(self.base_env())

        log_contents = self.read_log()
        self.assertIn("warning", log_contents.lower())
        self.assertIn("1", log_contents)


class StoreMissingOrCorruptFailOpenTests(MembershipFilterTestCase):
    """The highest-priority safety requirement in the whole task: a broken
    or missing membership store must never cause subscribers to silently
    stop receiving mail. Every one of these must return the full original
    list AND log loudly.
    """

    def test_missing_store_file_fails_open_with_full_list(self):
        self.write_subscribers("a@example.com", "b@example.com", "c@example.com")
        # Deliberately do not call write_store(): the file does not exist.

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["a@example.com", "b@example.com", "c@example.com"])

    def test_missing_store_file_logs_loudly(self):
        self.write_subscribers("a@example.com")

        subscriber_recipients(self.base_env())

        log_contents = self.read_log()
        self.assertTrue(log_contents.strip(), "expected a log line for the fail-open event")
        self.assertIn("UNAVAILABLE", log_contents)

    def test_corrupt_json_fails_open_with_full_list(self):
        self.write_subscribers("a@example.com", "b@example.com")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("{not valid json at all", encoding="utf-8")

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["a@example.com", "b@example.com"])

    def test_corrupt_json_logs_loudly(self):
        self.write_subscribers("a@example.com")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("{not valid json at all", encoding="utf-8")

        subscriber_recipients(self.base_env())

        self.assertIn("UNAVAILABLE", self.read_log())

    def test_malformed_member_entry_fails_open_with_full_list(self):
        # One bad row anywhere in the file must not take down the whole list.
        self.write_subscribers("a@example.com")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps({"version": 1, "members": [{"email": "a@example.com"}]}), encoding="utf-8")  # missing joined_at

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["a@example.com"])

    def test_non_dict_member_entry_fails_open_with_full_list(self):
        # Regression: a member row that isn't a JSON object at all (e.g. a
        # bare string from a bad manual edit) used to raise an uncaught
        # AttributeError out of Member.from_dict, bypassing the
        # MembershipStoreError catch below and crashing the real send path
        # instead of failing open like every other malformed-store case.
        self.write_subscribers("a@example.com", "b@example.com")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps({"version": 1, "members": ["not-a-dict"]}), encoding="utf-8")

        result = subscriber_recipients(self.base_env())

        self.assertEqual(result, ["a@example.com", "b@example.com"])

    def test_81_real_looking_subscribers_all_survive_a_missing_store(self):
        # Directly mirrors the real migration scale so a regression here is
        # caught in exactly the shape it would hurt production.
        emails = [f"user{i}@example.com" for i in range(81)]
        self.write_subscribers(*emails)

        result = subscriber_recipients(self.base_env())

        self.assertEqual(len(result), 81)
        self.assertEqual(set(result), set(emails))


class KillSwitchTests(MembershipFilterTestCase):
    def test_enforcement_zero_returns_full_list_even_with_expired_members(self):
        self.write_subscribers("expired@example.com", "active@example.com")
        self.write_store([
            _member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
            _member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC)),
        ])

        result = subscriber_recipients(self.base_env(MEMBERSHIP_ENFORCEMENT="0"))

        self.assertEqual(result, ["expired@example.com", "active@example.com"])

    def test_enforcement_zero_never_touches_the_store_file(self):
        # The kill switch must work even if the store itself is the thing on
        # fire; it must not require a successful store read to take effect.
        self.write_subscribers("a@example.com")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("{completely broken", encoding="utf-8")

        result = subscriber_recipients(self.base_env(MEMBERSHIP_ENFORCEMENT="0"))

        self.assertEqual(result, ["a@example.com"])

    def test_default_is_enforcement_on(self):
        self.write_subscribers("expired@example.com")
        self.write_store([_member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        env = self.base_env()
        env.pop("MEMBERSHIP_ENFORCEMENT", None)
        result = subscriber_recipients(env)

        self.assertEqual(result, [])


class AdminNeverFilteredTests(MembershipFilterTestCase):
    def test_admin_recipients_ignores_membership_store_entirely(self):
        self.write_admins("admin@example.com")
        # No membership store at all; if admin_recipients touched the
        # membership path this would raise/behave differently.
        result = admin_recipients(self.base_env())
        self.assertEqual(result, ["admin@example.com"])

    def test_admin_is_not_excludable_even_if_listed_as_expired_member(self):
        # Defensive: an admin address should never appear in the subscriber
        # membership store in practice, but even if it did (e.g. someone
        # reused an address), the admin route must not filter it.
        self.write_admins("admin@example.com")
        self.write_store([_member("admin@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        result = recipients_for_route(EmailRoute.ADMIN, self.base_env())

        self.assertEqual(result, ["admin@example.com"])

    def test_picks_update_route_filters_subscribers_but_keeps_admin(self):
        self.write_subscribers("expired@example.com")
        self.write_admins("admin@example.com")
        self.write_store([_member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        result = recipients_for_route(EmailRoute.PICKS_UPDATE, self.base_env())

        self.assertEqual(result, ["admin@example.com"])


class RoutePreviewTests(MembershipFilterTestCase):
    def test_preview_reports_included_excluded_and_counts(self):
        self.write_subscribers("active@example.com", "expired@example.com", "unknown@example.com")
        self.write_admins("admin@example.com")
        self.write_store([
            _member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC)),
            _member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
        ])

        preview = route_preview(EmailRoute.PICKS_UPDATE, self.base_env())

        self.assertEqual(preview["enforcement"], True)
        self.assertEqual(set(preview["included"]), {"active@example.com", "unknown@example.com", "admin@example.com"})
        self.assertEqual(len(preview["excluded"]), 1)
        self.assertEqual(preview["excluded"][0]["email"], "expired@example.com")
        self.assertEqual(preview["excluded"][0]["reason"], "expired")
        self.assertEqual(preview["counts"]["excluded"], 1)
        self.assertEqual(preview["counts"]["unknown_fail_open"], 1)

    def test_preview_is_json_serializable(self):
        self.write_subscribers("active@example.com")
        self.write_store([_member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC))])

        preview = route_preview(EmailRoute.PICKS_UPDATE, self.base_env())

        json.dumps(preview)  # must not raise

    def test_preview_for_admin_route_ignores_subscribers(self):
        self.write_subscribers("expired@example.com")
        self.write_admins("admin@example.com")
        self.write_store([_member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        preview = route_preview(EmailRoute.ADMIN, self.base_env())

        self.assertEqual(preview["included"], ["admin@example.com"])
        self.assertEqual(preview["excluded"], [])

    def test_preview_does_not_write_to_log(self):
        # route_preview is a read-only diagnostic used by the CLI/MCP/daily
        # admin status; it must not spam notify_routes.log on every
        # inspection the way an actual send does.
        self.write_subscribers("active@example.com")
        self.write_store([_member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC))])

        route_preview(EmailRoute.PICKS_UPDATE, self.base_env())

        self.assertEqual(self.read_log(), "")

    def test_preview_with_missing_store_still_fails_open(self):
        self.write_subscribers("a@example.com", "b@example.com")
        self.write_admins("admin@example.com")

        preview = route_preview(EmailRoute.PICKS_UPDATE, self.base_env())

        self.assertEqual(set(preview["included"]), {"a@example.com", "b@example.com", "admin@example.com"})
        self.assertEqual(preview["excluded"], [])
        self.assertIn("store_error", preview)


class LoggingContentTests(MembershipFilterTestCase):
    def test_every_call_logs_the_required_summary_line(self):
        self.write_subscribers("active@example.com")
        self.write_store([_member("active@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC))])

        subscriber_recipients(self.base_env())

        log_contents = self.read_log()
        self.assertIn("subscribers=1", log_contents)
        self.assertIn("active=1", log_contents)
        self.assertIn("excluded=0", log_contents)

    def test_excluded_addresses_are_listed_when_non_zero(self):
        self.write_subscribers("expired@example.com")
        self.write_store([_member("expired@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))])

        subscriber_recipients(self.base_env())

        log_contents = self.read_log()
        self.assertIn("expired@example.com", log_contents)


class ExplicitRootOverridesFrozenModuleRootTests(unittest.TestCase):
    """Regression test for a real bug found while wiring up admin_cli.py:
    quantcheck.notify_routes.ROOT (and gmail_api_notify.ROOT, which
    parse_recipients uses internally) are computed once at first import from
    the ambient QUANTCHECK_HOME, not from any `env`/`root` argument passed
    in later. `quantcheck-admin --root <X>` needs relative
    NOTIFY_EMAIL_FILE/NOTIFY_ADMIN_EMAIL_FILE/MEMBERSHIP_STORE_FILE settings
    to resolve against <X>, not against whatever ROOT happened to be frozen
    to at interpreter start (in production that's always /opt/quantcheck).
    Before the fix, this silently read the real repo's notify_recipients.txt
    (81 real subscribers) while operating against a throwaway tmp root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.intended_root = Path(self._tmp.name)
        (self.intended_root / "state").mkdir(parents=True)
        self.log_patch = patch("quantcheck.notify_routes.LOG_FILE", self.intended_root / "logs" / "notify_routes.log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_relative_notify_email_file_resolves_against_explicit_root_not_frozen_root(self):
        (self.intended_root / "notify_recipients.txt").write_text("tmp-only@example.com\n", encoding="utf-8")
        # Deliberately relative, and deliberately does NOT set QUANTCHECK_HOME
        # anywhere -- this is exactly the shape .env has in production
        # (NOTIFY_EMAIL_FILE=notify_recipients.txt, a bare relative name).
        env = {"NOTIFY_EMAIL_FILE": "notify_recipients.txt", "NOTIFY_EMAIL_TO": "", "MEMBERSHIP_ENFORCEMENT": "0"}

        with patch("quantcheck.notify_routes.ROOT", Path("/definitely/not/the/intended/root")):
            result = subscriber_recipients(env, root=self.intended_root)

        self.assertEqual(result, ["tmp-only@example.com"])

    def test_route_preview_membership_store_resolves_against_explicit_root(self):
        from quantcheck.membership_store import MembershipStore, save_store

        store = MembershipStore(path=self.intended_root / "state" / "memberships.json", members=[])
        save_store(store, backup=False)
        (self.intended_root / "notify_recipients.txt").write_text("a@example.com\n", encoding="utf-8")
        env = {"NOTIFY_EMAIL_FILE": "notify_recipients.txt", "NOTIFY_EMAIL_TO": "", "NOTIFY_ADMIN_EMAIL_FILE": "", "NOTIFY_ADMIN_EMAIL_TO": ""}

        with patch("quantcheck.notify_routes.ROOT", Path("/definitely/not/the/intended/root")):
            preview = route_preview(EmailRoute.PICKS_UPDATE, env, root=self.intended_root)

        # Store loaded successfully (from the intended root) with nobody in
        # it: "a@example.com" is unknown-to-store, not a store_error.
        self.assertNotIn("store_error", preview)
        self.assertEqual(preview["counts"]["unknown_fail_open"], 1)
        self.assertEqual(preview["included"], ["a@example.com"])

    def test_admin_recipients_relative_file_resolves_against_explicit_root(self):
        (self.intended_root / "notify_admin_recipients.txt").write_text("admin@example.com\n", encoding="utf-8")
        env = {"NOTIFY_ADMIN_EMAIL_FILE": "notify_admin_recipients.txt", "NOTIFY_ADMIN_EMAIL_TO": ""}

        with patch("quantcheck.notify_routes.ROOT", Path("/definitely/not/the/intended/root")):
            result = admin_recipients(env, root=self.intended_root)

        self.assertEqual(result, ["admin@example.com"])

    def test_omitting_root_still_falls_back_to_module_root_unchanged(self):
        # Backward compatibility: every pre-existing caller that never
        # passes root= must see byte-for-byte the same behavior as before
        # this parameter existed.
        fake_root = Path(self._tmp.name)
        (fake_root / "notify_recipients.txt").write_text("fallback@example.com\n", encoding="utf-8")
        env = {"NOTIFY_EMAIL_FILE": "notify_recipients.txt", "NOTIFY_EMAIL_TO": "", "MEMBERSHIP_ENFORCEMENT": "0"}

        with patch("quantcheck.notify_routes.ROOT", fake_root):
            result = subscriber_recipients(env)  # no root= kwarg at all

        self.assertEqual(result, ["fallback@example.com"])



class PaymentHistoryDoesNotAffectFilteringTests(MembershipFilterTestCase):
    """History entries gained free-form `payment`/`mode` keys; routing must
    be unaffected, and unknown/odd history must never break fail-open.
    """

    def test_member_with_payment_history_is_filtered_normally(self):
        self.write_subscribers("paid@example.com", "lapsed@example.com", "unknown@example.com")
        paid = _member("paid@example.com", expires_at=datetime(2099, 1, 1, tzinfo=UTC))
        paid.add_history(action="add", months=None, expires_at=paid.expires_at, actor="t", extra={"mode": "align", "payment": {"amount": 9, "currency": "CNY"}})
        paid.add_history(action="payment", months=None, expires_at=paid.expires_at, actor="t", extra={"payment": {"amount": 1}})
        lapsed = _member("lapsed@example.com", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        lapsed.add_history(action="payment", months=None, expires_at=lapsed.expires_at, actor="t", extra={"payment": {"amount": 1}})
        self.write_store([paid, lapsed])

        self.assertEqual(subscriber_recipients(self.base_env()), ["paid@example.com", "unknown@example.com"])


if __name__ == "__main__":
    unittest.main()
