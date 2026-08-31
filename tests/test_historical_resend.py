import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook


def picks(fetched_at, weekly_date, symbols):
    return {
        "fetched_at": fetched_at,
        "source": "https://quantgt.io",
        "monthly": {"pick_date": "Updated on August 1, 2026", "rows": [{"symbol": "DELL"}]},
        "weekly": {
            "pick_date": weekly_date,
            "kind": "watchlist",
            "rows": [{"symbol": symbol} for symbol in symbols],
        },
    }


class HistoricalResendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("state/raw", "output", "screenshots"):
            (self.root / name).mkdir(parents=True)

        old = picks("2026-08-08T16:00:44", "Updated on Jul 31, 2026", ["DELL", "ARM"])
        new = picks("2026-08-09T16:01:01", "Updated on Aug 7, 2026", ["DELL", "FROG"])
        (self.root / "state/raw/picks_raw_2026-08-08_120044.json").write_text(json.dumps(old))
        (self.root / "state/raw/picks_raw_2026-08-09_120101.json").write_text(json.dumps(new))
        workbook = Workbook()
        portfolio = workbook.active
        portfolio.title = "Portfolio"
        for _ in range(5):
            portfolio.append([])
        portfolio.append(["Rank", "Symbol"])
        portfolio.append([1, "DELL"])
        watchlist = workbook.create_sheet("Weekly Watchlist")
        for _ in range(5):
            watchlist.append([])
        watchlist.append(["Rank", "Symbol"])
        watchlist.append([1, "DELL"])
        watchlist.append([2, "FROG"])
        workbook.save(self.root / "output/quantgt_picks_report_2026-08-09_160101.xlsx")
        (self.root / "screenshots/portfolio_2026-08-09_120101.png").write_bytes(b"portfolio")
        (self.root / "screenshots/watchlist_2026-08-09_120101.png").write_bytes(b"watchlist")

    def tearDown(self):
        self.tmp.cleanup()

    def test_selects_snapshot_by_internal_pick_date_and_reconstructs_diff(self):
        from quantcheck.historical_resend import prepare_resend

        plan = prepare_resend(self.root, "Updated on Aug 7, 2026")

        self.assertEqual(plan.raw_path.name, "picks_raw_2026-08-09_120101.json")
        self.assertEqual(plan.diff["weekly"]["added"], ["FROG"])
        self.assertEqual(plan.diff["weekly"]["removed"], ["ARM"])
        self.assertIn("- Added: FROG", plan.body)
        self.assertIn("- Removed: ARM", plan.body)

    def test_binds_excel_and_both_screenshots_from_same_fetch(self):
        from quantcheck.historical_resend import prepare_resend

        plan = prepare_resend(self.root, "Updated on Aug 7, 2026")

        self.assertEqual(
            [path.name for path in plan.attachments],
            [
                "quantgt_picks_report_2026-08-09_160101.xlsx",
                "portfolio_2026-08-09_120101.png",
                "watchlist_2026-08-09_120101.png",
            ],
        )

    def test_fails_closed_when_matching_attachment_is_missing(self):
        from quantcheck.historical_resend import ResendValidationError, prepare_resend

        (self.root / "screenshots/watchlist_2026-08-09_120101.png").unlink()

        with self.assertRaisesRegex(ResendValidationError, "missing same-run attachment"):
            prepare_resend(self.root, "Updated on Aug 7, 2026")

    def test_fails_closed_when_excel_symbols_do_not_match_raw(self):
        from quantcheck.historical_resend import ResendValidationError, prepare_resend

        path = self.root / "output/quantgt_picks_report_2026-08-09_160101.xlsx"
        workbook = load_workbook(path)
        sheet = workbook["Weekly Watchlist"]
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value == "FROG":
                    cell.value = "WRONG"
        workbook.save(path)

        with self.assertRaisesRegex(ResendValidationError, "Excel symbols do not match raw snapshot"):
            prepare_resend(self.root, "Updated on Aug 7, 2026")

    def test_send_requires_exact_date_confirmation(self):
        from quantcheck.historical_resend import ResendValidationError, execute_resend, prepare_resend

        plan = prepare_resend(self.root, "Updated on Aug 7, 2026")
        with patch("quantcheck.historical_resend.send_email_per_recipient") as sender:
            with self.assertRaisesRegex(ResendValidationError, "confirmation date"):
                execute_resend(plan, ["user@example.com"], confirm_date="Updated on Jul 31, 2026")
            sender.assert_not_called()

    def test_send_uses_validated_body_attachments_and_private_recipients(self):
        from quantcheck.historical_resend import execute_resend, prepare_resend

        plan = prepare_resend(self.root, "Updated on Aug 7, 2026")
        with patch(
            "quantcheck.historical_resend.send_email_per_recipient",
            return_value=(["user@example.com"], []),
        ) as sender:
            delivered, failed = execute_resend(
                plan,
                ["user@example.com"],
                confirm_date="Updated on Aug 7, 2026",
            )

        self.assertEqual(delivered, ["user@example.com"])
        self.assertEqual(failed, [])
        sender.assert_called_once_with(
            "Quant GT Picks Updated",
            plan.body,
            to=["user@example.com"],
            attachments=list(plan.attachments),
            html=plan.html,
            retries=2,
        )

    def test_fails_closed_when_update_has_no_diff(self):
        from quantcheck.historical_resend import ResendValidationError, prepare_resend

        duplicate = picks("2026-08-10T13:00:43", "Updated on Aug 7, 2026", ["DELL", "FROG"])
        (self.root / "state/raw/picks_raw_2026-08-10_090043.json").write_text(json.dumps(duplicate))

        plan = prepare_resend(self.root, "Updated on Aug 7, 2026")
        self.assertTrue(plan.diff["changed"])
        self.assertIn("Changes:", plan.body)


if __name__ == "__main__":
    unittest.main()
