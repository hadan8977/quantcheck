import unittest

from quantcheck.picks_report import clean_detail_values
from quantcheck.validation import validate_member_picks_data


def valid_monthly_row(symbol="M1"):
    return {
        "symbol": symbol,
        "company": "Monthly One Inc.",
        "current_price": "$20.00",
        "return": "+12.30%",
        "sector": "Technology",
        "gt_score": "4.50/5",
        "buy_or_entry_price": "$18.00",
        "next_earnings": "2026-06-15",
        "analyst_signal": "Buy +0.25",
    }


def valid_weekly_row(symbol="W1"):
    return {
        "symbol": symbol,
        "company": "Weekly One Inc.",
        "current_price": "$10.00",
        "buy_or_entry_price": "$9.50",
        "sector": "Technology",
        "gt_score": "4.20/5",
        "next_earnings": "2026-06-01",
        "analyst_signal": "Buy +0.20",
    }


def valid_capture():
    return {
        "monthly": {"pick_date": "May 2026", "rows": [valid_monthly_row()]},
        "weekly": {
            "pick_date": "05/22/26",
            "rows": [valid_weekly_row(f"W{i}") for i in range(1, 11)],
        },
    }


class ValidationTests(unittest.TestCase):
    def test_valid_member_capture_passes(self):
        validate_member_picks_data(valid_capture())

    def test_demo_weekly_signature_is_rejected(self):
        data = valid_capture()
        data["weekly"]["pick_date"] = "05/15/26"
        data["weekly"]["rows"] = [valid_weekly_row(symbol) for symbol in ["SNDK", "LITE", "AAOI", "FORM", "VIAV", "ENPH"]]

        with self.assertRaisesRegex(RuntimeError, "demo Weekly Picks"):
            validate_member_picks_data(data)

    def test_partial_weekly_details_are_rejected(self):
        data = valid_capture()
        data["weekly"]["rows"][0]["analyst_signal"] = ""

        with self.assertRaisesRegex(RuntimeError, "incomplete detail rows"):
            validate_member_picks_data(data)

    def test_monthly_detail_without_buy_price_passes_when_other_details_loaded(self):
        data = valid_capture()
        data["monthly"]["rows"][0]["buy_or_entry_price"] = ""

        validate_member_picks_data(data)

    def test_monthly_missing_loaded_detail_field_is_rejected(self):
        data = valid_capture()
        data["monthly"]["rows"][0]["analyst_signal"] = ""

        with self.assertRaisesRegex(RuntimeError, "incomplete loaded rows"):
            validate_member_picks_data(data)

    def test_monthly_analyst_signal_with_description_is_rejected(self):
        data = valid_capture()
        data["monthly"]["rows"][0]["analyst_signal"] = (
            "Sell -0.29 Sandisk Corporation develops data storage devices. More Headlines"
        )

        with self.assertRaisesRegex(RuntimeError, "malformed analyst signal"):
            validate_member_picks_data(data)

    def test_weekly_detail_without_buy_price_passes_when_other_details_loaded(self):
        data = valid_capture()
        data["weekly"]["rows"][0]["buy_or_entry_price"] = ""

        validate_member_picks_data(data)

    def test_new_layout_without_detail_rows_passes(self):
        data = {
            "monthly": {"pick_date": "May Holdings 05/01/26 - now", "rows": [valid_monthly_row()]},
            "weekly": {
                "pick_date": "05/22/26",
                "rows": [valid_weekly_row(f"W{i}") for i in range(1, 11)],
            },
        }

        validate_member_picks_data(data)

    def test_watchlist_layout_rejects_card_fields_without_dialog_details(self):
        data = {
            "monthly": {"pick_date": "Updated July 1, 2026", "rows": [valid_monthly_row()]},
            "weekly": {
                "kind": "watchlist",
                "pick_date": "Updated on Jun 26, 2026",
                "rows": [
                    {
                        "symbol": f"W{i}",
                        "company": "Watchlist One Inc.",
                        "current_price": "$10.00",
                        "sector": "Technology",
                        "source_kind": "watchlist",
                    }
                    for i in range(1, 11)
                ],
            },
        }

        with self.assertRaisesRegex(RuntimeError, "incomplete detail rows"):
            validate_member_picks_data(data)

    def test_watchlist_layout_passes_with_dialog_details(self):
        data = valid_capture()
        data["weekly"]["kind"] = "watchlist"
        for row in data["weekly"]["rows"]:
            row.update({
                "momentum": "1.20/2",
                "relative_strength": "3.00/3",
                "gt_score_source": "dialog_components",
            })

        validate_member_picks_data(data)

    def test_watchlist_api_score_passes_without_retired_component_fields(self):
        data = valid_capture()
        data["weekly"]["kind"] = "watchlist"
        for row in data["weekly"]["rows"]:
            row["gt_score_source"] = "weekly_api_score"

        validate_member_picks_data(data)

    def test_watchlist_accepts_explicitly_unavailable_source_details(self):
        data = valid_capture()
        data["weekly"]["kind"] = "watchlist"
        for row in data["weekly"]["rows"]:
            row["gt_score_source"] = "weekly_api_score"
        row = data["weekly"]["rows"][0]
        row["next_earnings"] = ""
        row["next_earnings_unavailable"] = True
        row["analyst_signal"] = ""
        row["analyst_signal_unavailable"] = True

        validate_member_picks_data(data)

    def test_watchlist_detail_cleaning_preserves_unavailable_marker_types(self):
        cleaned = clean_detail_values({
            "next_earnings": "  —  ",
            "next_earnings_unavailable": True,
            "analyst_signal_unavailable": False,
        })

        self.assertEqual(cleaned["next_earnings"], "—")
        self.assertIs(cleaned["next_earnings_unavailable"], True)
        self.assertIs(cleaned["analyst_signal_unavailable"], False)

    def test_watchlist_layout_still_requires_monthly_detail_quality(self):
        data = {
            "monthly": {"pick_date": "Updated July 1, 2026", "rows": [valid_monthly_row()]},
            "weekly": {
                "kind": "watchlist",
                "pick_date": "Updated on Jun 26, 2026",
                "rows": [
                    {"symbol": f"W{i}", "company": "Watchlist One Inc.", "current_price": "$10.00", "sector": "Technology"}
                    for i in range(1, 11)
                ],
            },
        }
        data["monthly"]["rows"][0]["analyst_signal"] = ""

        with self.assertRaisesRegex(RuntimeError, "incomplete loaded rows"):
            validate_member_picks_data(data)

    def test_partial_weekly_top10_capture_is_rejected(self):
        data = valid_capture()
        data["weekly"]["rows"] = [valid_weekly_row("BKR")]

        with self.assertRaisesRegex(RuntimeError, "expected near-complete Weekly Top 10"):
            validate_member_picks_data(data)


if __name__ == "__main__":
    unittest.main()
