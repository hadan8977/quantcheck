import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import unittest

from quantcheck.scrape_parse import rows_from_matrix
from quantcheck.validation import validate_member_picks_data


class ScrapeParseAndValidationTests(unittest.TestCase):
    def test_current_weekly_table_without_rating_maps_price_sector_score_correctly(self):
        matrix = [
            ["COMPANY", "SYMBOL", "PRICE", "SECTOR", "GT SCORE", ""],
            ["Sandisk Corporation", "SNDK", "$1,431.67", "Electronic Technology", "5.01/5", ""],
        ]

        self.assertEqual(
            rows_from_matrix(matrix, "weekly"),
            [{
                "company": "Sandisk Corporation",
                "symbol": "SNDK",
                "buy_or_entry_price": "$1,431.67",
                "sector": "Electronic Technology",
                "gt_score": "5.01/5",
            }],
        )

    def test_missing_loaded_detail_fields_fails_validation_before_user_notification(self):
        data = {
            "monthly": {
                "pick_date": "Updated May 1, 2026",
                "rows": [{
                    "symbol": "AAOI",
                    "company": "Applied Optoelectronics, Inc.",
                    "current_price": "$177.62",
                    "return": "+97.02%",
                    "sector": "Electronic Technology",
                    "gt_score": "4.98/5",
                    "buy_or_entry_price": "$90.15",
                    # missing next_earnings and analyst_signal indicates a partial/detail-load failure
                }],
            },
            "weekly": {
                "pick_date": "Week of May 25, 2026",
                "rows": [
                    {
                        "symbol": f"W{i}",
                        "company": "Weekly One Inc.",
                        "current_price": "$1,589.55",
                        "buy_or_entry_price": "$1,431.67",
                        "sector": "Electronic Technology",
                        "gt_score": "5.01/5",
                        "next_earnings": "Aug 13, 2026",
                        "analyst_signal": "Strong Buy +0.51",
                    }
                    for i in range(1, 11)
                ],
            },
        }

        with self.assertRaisesRegex(RuntimeError, "incomplete loaded rows"):
            validate_member_picks_data(data)


if __name__ == "__main__":
    unittest.main()
