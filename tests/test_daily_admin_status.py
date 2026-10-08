import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck import daily_admin_status as das
from quantcheck.membership_store import Member, MembershipStore, save_store

UTC = timezone.utc


class DailyAdminStatusTestCase(unittest.TestCase):
    """Every test patches ROOT/STATE/LOG_FILE to a tmp dir -- daily_admin_status
    is exactly the kind of module the project's fixture-pollution incident
    warned about (it both reads state/ and writes logs/), so none of this
    may ever touch the real /opt/quantcheck paths.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)

        self._patches = [
            patch("quantcheck.daily_admin_status.ROOT", self.root),
            patch("quantcheck.daily_admin_status.STATE", self.root / "state"),
            patch("quantcheck.daily_admin_status.LOG_FILE", self.root / "logs" / "daily_admin_status.log"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def write_store(self, members):
        save_store(MembershipStore(path=self.root / "state" / "memberships.json", members=list(members)), backup=False)

    def base_env(self, **overrides) -> dict:
        env = {"OFFICIAL_MAIL_ENABLED": "0", "NOTIFY_ADMIN_EMAIL_TO": "admin@example.com", "NOTIFY_ADMIN_EMAIL_FILE": ""}
        env.update(overrides)
        return env


class MembershipCardsTests(DailyAdminStatusTestCase):
    def test_no_store_yet_reports_zero_counts_not_an_error(self):
        cards = das.membership_cards(self.base_env())
        by_label = {c["label"]: c for c in cards}
        self.assertEqual(by_label["Members Active"]["value"], 0)
        self.assertEqual(by_label["Members Expired"]["value"], 0)

    def test_enforcement_on_by_default(self):
        cards = das.membership_cards(self.base_env())
        by_label = {c["label"]: c for c in cards}
        self.assertEqual(by_label["Membership Enforcement"]["value"], "on")
        self.assertEqual(by_label["Membership Enforcement"]["tone"], "neutral")

    def test_enforcement_off_is_flagged_as_error_tone(self):
        cards = das.membership_cards(self.base_env(MEMBERSHIP_ENFORCEMENT="0"))
        by_label = {c["label"]: c for c in cards}
        self.assertIn("OFF", by_label["Membership Enforcement"]["value"])
        self.assertEqual(by_label["Membership Enforcement"]["tone"], "error")

    def test_counts_and_thresholds(self):
        now = datetime.now(UTC)
        self.write_store(
            [
                Member(email="active@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=100)),
                Member(email="soon@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=3)),
                Member(email="mid@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=10)),
                Member(email="expired@example.com", status="active", joined_at=now, expires_at=now - timedelta(days=5)),
                Member(email="cancelled@example.com", status="cancelled", joined_at=now, expires_at=now + timedelta(days=100)),
            ]
        )

        cards = das.membership_cards(self.base_env())
        by_label = {c["label"]: c for c in cards}

        self.assertEqual(by_label["Members Active"]["value"], 3)  # active, soon, mid
        self.assertEqual(by_label["Members Expiring <=7d"]["value"], 1)
        self.assertEqual(by_label["Members Expiring <=7d"]["tone"], "warning")
        self.assertEqual(by_label["Members Expiring <=14d"]["value"], 2)
        self.assertEqual(by_label["Members Expired"]["value"], 1)
        self.assertEqual(by_label["Members Expired"]["tone"], "error")
        self.assertIn("soon@example.com", by_label["Expiring Within 14d"]["value"])
        self.assertIn("mid@example.com", by_label["Expiring Within 14d"]["value"])
        self.assertNotIn("active@example.com", by_label["Expiring Within 14d"]["value"])

    def test_no_one_expiring_shows_neutral_tone_and_none_placeholder(self):
        now = datetime.now(UTC)
        self.write_store([Member(email="active@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=100))])

        cards = das.membership_cards(self.base_env())
        by_label = {c["label"]: c for c in cards}

        self.assertEqual(by_label["Members Expiring <=14d"]["tone"], "neutral")
        self.assertEqual(by_label["Expiring Within 14d"]["value"], "none")

    def test_corrupt_store_reports_error_card_instead_of_crashing(self):
        (self.root / "state" / "memberships.json").write_text("{not valid json", encoding="utf-8")
        cards = das.membership_cards(self.base_env())
        by_label = {c["label"]: c for c in cards}
        self.assertEqual(by_label["Membership"]["tone"], "error")
        self.assertIn("memberships.json", by_label["Membership"]["value"])


class BuildStatusIncludesMembershipTests(DailyAdminStatusTestCase):
    def test_build_status_body_includes_membership_section(self):
        now = datetime.now(UTC)
        self.write_store([Member(email="soon@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=2))])

        subject, body, html = das.build_status(self.base_env())

        self.assertIn("Members Expiring <=7d: 1", body)
        self.assertIn("Membership Enforcement: on", body)
        self.assertIn("soon@example.com", html)

    def test_build_status_is_still_well_formed_with_no_membership_data(self):
        subject, body, html = das.build_status(self.base_env())
        self.assertTrue(subject.startswith("Quant GT Daily Admin Status"))
        self.assertIn("Members Active: 0", body)


class SendDailyStatusNeverTouchesRealInfrastructureTests(DailyAdminStatusTestCase):
    def test_send_uses_patched_log_file_and_mocked_delivery(self):
        now = datetime.now(UTC)
        self.write_store([Member(email="soon@example.com", status="active", joined_at=now, expires_at=now + timedelta(days=2))])

        with patch("quantcheck.daily_admin_status.load_env", return_value=self.base_env()), \
             patch("quantcheck.daily_admin_status.deliver_email", return_value=True) as deliver:
            ok = das.send_daily_status()

        self.assertTrue(ok)
        deliver.assert_called_once()
        log_contents = (self.root / "logs" / "daily_admin_status.log").read_text(encoding="utf-8")
        self.assertIn("sent", log_contents)


if __name__ == "__main__":
    unittest.main()
