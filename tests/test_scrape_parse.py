import unittest

from quantcheck.scrape_parse import (
    extract_pick_date,
    parse_watchlist_dialog_text,
    rows_from_card_texts,
    rows_from_matrix,
)


class ScrapeParseTests(unittest.TestCase):
    def test_rows_from_new_monthly_table_header(self):
        matrix = [
            ["Company", "Symbol", "Held Since", "Price", "Return", "Sector", "Rating", "GT Score"],
            ["Acme Corp", "ACME", "05/01/26", "$123.45", "+12.5%", "Technology", "Buy", "87"],
            ["$123.45 P/E (TTM) 20 Market Cap $10B", "", "", "", "", "", "", ""],
        ]

        self.assertEqual(
            rows_from_matrix(matrix, "monthly"),
            [{
                "company": "Acme Corp",
                "symbol": "ACME",
                "held_since": "05/01/26",
                "current_price": "$123.45",
                "return": "+12.5%",
                "sector": "Technology",
                "rating": "Buy",
                "gt_score": "87",
            }],
        )

    def test_rows_from_new_weekly_table_header(self):
        matrix = [
            ["Company", "Symbol", "Sector", "Rating", "GT Score"],
            ["Beta Inc", "BETA", "Healthcare", "Strong Buy", "91"],
        ]

        self.assertEqual(
            rows_from_matrix(matrix, "weekly"),
            [{"company": "Beta Inc", "symbol": "BETA", "sector": "Healthcare", "rating": "Strong Buy", "gt_score": "91"}],
        )

    def test_rows_from_logged_in_monthly_table_header(self):
        matrix = [
            ["COMPANY", "SYMBOL", "HELD SINCE", "PRICE", "RETURN", "SECTOR", "RATING", "GT SCORE", ""],
            [
                "Applied Optoelectronics, Inc.",
                "AAOI",
                "2026-04-01",
                "$181.49",
                "+101.31%",
                "Electronic Technology",
                "Strong Buy",
                "4.98/5",
                "",
            ],
        ]

        self.assertEqual(
            rows_from_matrix(matrix, "monthly"),
            [{
                "company": "Applied Optoelectronics, Inc.",
                "symbol": "AAOI",
                "held_since": "2026-04-01",
                "current_price": "$181.49",
                "return": "+101.31%",
                "sector": "Electronic Technology",
                "rating": "Strong Buy",
                "gt_score": "4.98/5",
            }],
        )

    def test_rows_from_card_layout(self):
        cards = ["Company: Gamma Ltd Symbol: GAMA Sector: Energy Rating: Buy GT Score: 82"]

        self.assertEqual(
            rows_from_card_texts(cards, "weekly"),
            [{"company": "Gamma Ltd", "symbol": "GAMA", "sector": "Energy", "rating": "Buy", "gt_score": "82"}],
        )

    def test_rows_from_watchlist_card_layout(self):
        cards = ["SNDK Sandisk Corporation $2,032.22 Electronic Technology"]

        self.assertEqual(
            rows_from_card_texts(cards, "weekly"),
            [{
                "symbol": "SNDK",
                "company": "Sandisk Corporation",
                "current_price": "$2,032.22",
                "sector": "Electronic Technology",
                "source_kind": "watchlist",
            }],
        )

    def test_watchlist_dialog_details_restore_legacy_weekly_fields(self):
        text = (
            "SNDK Electronic Technology Sandisk Corporation PRICE $1,915.92 "
            "$1915.92 +596.09% 1M 6M 1Y YTD SNDK : $618.82 "
            "P/E (TTM) 66.60 Market Cap $283.21B Revenue (TTM) $13.18B "
            "Revenue Growth (YoY) +82.76% Next Earnings Aug 13, 2026 "
            "Analyst Consensus Buy +0.24 Momentum 1.96/2 Relative Strength 3.00/3 "
            "Sandisk Corporation develops data storage products. More Headlines"
        )

        details = parse_watchlist_dialog_text(text, "SNDK")

        self.assertEqual(details["buy_or_entry_price"], "$618.82")
        self.assertEqual(details["market_cap"], "$283.21B")
        self.assertEqual(details["next_earnings"], "Aug 13, 2026")
        self.assertEqual(details["analyst_signal"], "Buy +0.24")
        self.assertEqual(details["momentum"], "1.96/2")
        self.assertEqual(details["relative_strength"], "3.00/3")
        self.assertEqual(details["gt_score"], "4.96/5")

    def test_analyst_consensus_is_a_detail_label_boundary(self):
        cards = ["Company: Gamma Ltd Symbol: GAMA Sector: Energy Analyst Consensus Buy +0.12 Momentum 1.9/2 GT Score: 82"]

        self.assertEqual(rows_from_card_texts(cards, "weekly")[0]["sector"], "Energy")

    def test_monthly_holdings_date_is_supported(self):
        text = "Portfolio Return May Holdings 05/01/26 - now Company Symbol Held Since"

        self.assertEqual(extract_pick_date(text, "monthly"), "May Holdings 05/01/26 - now")

    def test_monthly_updated_date_is_supported(self):
        text = "Portfolio Return Latest Holdings Updated May 1, 2026 MTD +14.76% COMPANY SYMBOL"

        self.assertEqual(extract_pick_date(text, "monthly"), "Updated May 1, 2026")

    def test_monthly_updated_on_date_is_supported(self):
        text = "Portfolio Return Latest Holdings Updated on July 1, 2026 MTD -4.38% COMPANY SYMBOL"

        self.assertEqual(extract_pick_date(text, "monthly"), "Updated on July 1, 2026")

    def test_week_of_date_is_supported(self):
        text = "Weekly Picks Guidance only Week of May 25, 2026 COMPANY SYMBOL SECTOR"

        self.assertEqual(extract_pick_date(text, "weekly"), "Week of May 25, 2026")
    def test_weekly_updated_on_abbreviated_date_is_supported(self):
        text = "Weekly Picks Guidance only Updated on Jun 5, 2026 WTD return +0.00% COMPANY SYMBOL"

        self.assertEqual(extract_pick_date(text, "weekly"), "Updated on Jun 5, 2026")

    def test_weekly_updated_abbreviated_date_is_supported(self):
        text = "Weekly Picks Updated Jun 5, 2026 COMPANY SYMBOL"

        self.assertEqual(extract_pick_date(text, "weekly"), "Updated Jun 5, 2026")


if __name__ == "__main__":
    unittest.main()
