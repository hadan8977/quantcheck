import unittest

from quantcheck.picks_report import merge_watchlist_api_scores
from quantcheck.scrape_parse import (
    extract_pick_date,
    parse_analyst_signal_text,
    parse_watchlist_dialog_text,
    rows_from_card_texts,
    rows_from_matrix,
)


class ScrapeParseTests(unittest.TestCase):
    def test_analyst_signal_strips_company_description_and_headlines(self):
        raw = (
            "Sell -0.29 Sandisk Corporation develops data storage devices. "
            "More Headlines Stock Market Today"
        )

        self.assertEqual(parse_analyst_signal_text(raw), "Sell -0.29")

    def test_analyst_signal_rejects_text_without_numeric_source_value(self):
        self.assertEqual(parse_analyst_signal_text("Sell Sandisk Corporation develops products"), "")

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

    def test_rows_from_monthly_table_header_using_entry_date_label(self):
        # Quant GT renamed the Portfolio holdings table's "Held Since" column
        # to "Entry Date"; the internal field name stays held_since.
        matrix = [
            ["SYMBOL", "COMPANY", "ENTRY DATE", "PRICE", "RETURN", "SECTOR", "GT SCORE"],
            ["DELL", "Dell Technologies Inc.", "2026-08-03", "$456.24", "+14.73%", "Electronic Technology", "4.78/5"],
        ]

        rows = rows_from_matrix(matrix, "monthly")

        self.assertEqual(rows[0]["held_since"], "2026-08-03")

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

    def test_rows_from_watchlist_card_layout_without_price(self):
        # Quant GT's Watchlist card stopped rendering an inline $price; only
        # the symbol is safely extractable from the remaining text, and the
        # row is still tagged watchlist so downstream code enriches it from
        # the authenticated Watchlist API instead of rejecting it outright.
        cards = ["CORT Corcept Therapeutics Incorporated Health Technology"]

        self.assertEqual(
            rows_from_card_texts(cards, "weekly"),
            [{"symbol": "CORT", "source_kind": "watchlist"}],
        )

    def test_legacy_weekly_card_with_gt_score_is_not_treated_as_priceless_watchlist(self):
        cards = ["Company: Gamma Ltd Symbol: GAMA Sector: Energy Rating: Buy GT Score: 82"]

        rows = rows_from_card_texts(cards, "weekly")

        self.assertEqual(rows, [{"company": "Gamma Ltd", "symbol": "GAMA", "sector": "Energy", "rating": "Buy", "gt_score": "82"}])

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

    def test_watchlist_dialog_current_layout_extracts_consensus_without_retired_scores(self):
        text = (
            "SNDK Electronic Technology Sandisk Corporation PRICE $1,505.00 "
            "SNDK : $702.49 P/E (TTM) 55.98 Market Cap $237.91B "
            "Revenue (TTM) $13.18B Revenue Growth (YoY) +82.76% "
            "Next Earnings Aug 5, 2026 Analyst Consensus Neutral -0.09 "
            "Sandisk Corporation develops data storage products. More Headlines Close"
        )

        details = parse_watchlist_dialog_text(text, "SNDK")

        self.assertEqual(details["next_earnings"], "Aug 5, 2026")
        self.assertEqual(details["analyst_signal"], "Neutral -0.09")
        self.assertNotIn("gt_score", details)

    def test_watchlist_dialog_marks_source_unavailable_details(self):
        text = (
            "CORT Health Technology Corcept Therapeutics Incorporated PRICE $90.89 "
            "P/E (TTM) — Market Cap $10.25B Revenue (TTM) — "
            "Revenue Growth (YoY) — Next Earnings — Analyst Consensus — "
            "Headlines Corcept Therapeutics lifted its outlook Close"
        )

        details = parse_watchlist_dialog_text(text, "CORT")

        self.assertEqual(details["next_earnings"], "")
        self.assertTrue(details["next_earnings_unavailable"])
        self.assertEqual(details["analyst_signal"], "")
        self.assertTrue(details["analyst_signal_unavailable"])

    def test_watchlist_dialog_unavailable_analyst_consensus_not_swallowed_by_company_blurb(self):
        # Regression (2026-09-08, APGE): when Momentum/Relative Strength have
        # no data, Quant GT renders no label text for them at all -- not even
        # a "-". value_after's boundary search for "Analyst Consensus" then
        # has no nearby label to stop at and runs all the way to "More
        # Headlines", swallowing the entire company-description paragraph in
        # between into what should have been a lone "-". That made
        # analyst_signal_unavailable False (it was checking whether the
        # *whole swallowed blob* equals "-"), which made this stock look
        # like a genuine scrape failure ("missing analyst_signal") every run
        # for days, instead of a legitimately-unavailable field.
        text = (
            "APGE Health Technology Apogee Therapeutics, Inc. PRICE $135.07 — "
            "1M 6M 1Y YTD P/E (TTM) — Market Cap $10.22B Revenue (TTM) — "
            "Revenue Growth (YoY) — Next Earnings — Analyst Consensus — "
            "Apogee Therapeutics, Inc. develops and commercializes biologic "
            "therapies for immunological and inflammatory (I&I) diseases for "
            "patients and caregivers. The company's pipeline includes "
            "products, such as ZUMILOKIBART (APG777), a monoclonal antibody "
            "for atopic dermatitis, asthma, and eosinophilic… More Headlines Close"
        )

        details = parse_watchlist_dialog_text(text, "APGE")

        self.assertEqual(details["analyst_signal"], "")
        self.assertTrue(details["analyst_signal_unavailable"])
        self.assertEqual(details["next_earnings"], "")
        self.assertTrue(details["next_earnings_unavailable"])

    def test_merge_watchlist_api_scores_uses_native_score(self):
        rows = [{"symbol": "SNDK"}, {"symbol": "MXL"}]
        api_rows = [
            {"ticker": "SNDK", "score": 4.8592},
            {"ticker": "MXL", "score": 4.8452},
        ]

        merged = merge_watchlist_api_scores(rows, api_rows)

        self.assertEqual(merged[0]["gt_score"], "4.86/5")
        self.assertEqual(merged[0]["gt_score_source"], "weekly_api_score")
        self.assertEqual(merged[1]["gt_score"], "4.85/5")

    def test_merge_watchlist_api_scores_backfills_missing_company_sector_price(self):
        rows = [{"symbol": "CORT", "source_kind": "watchlist"}]
        api_rows = [{
            "ticker": "CORT",
            "name": "Corcept Therapeutics Incorporated",
            "sector": "Health Technology",
            "price": None,
            "sell_price": 113.88,
            "score": 4.7075,
        }]

        merged = merge_watchlist_api_scores(rows, api_rows)

        self.assertEqual(merged[0]["company"], "Corcept Therapeutics Incorporated")
        self.assertEqual(merged[0]["sector"], "Health Technology")
        self.assertEqual(merged[0]["current_price"], "$113.88")
        self.assertEqual(merged[0]["gt_score"], "4.71/5")

    def test_merge_watchlist_api_scores_does_not_override_card_supplied_fields(self):
        rows = [{"symbol": "SNDK", "company": "Card Company", "sector": "Card Sector", "current_price": "$2,032.22"}]
        api_rows = [{"ticker": "SNDK", "name": "API Company", "sector": "API Sector", "sell_price": 1.0, "score": 4.5}]

        merged = merge_watchlist_api_scores(rows, api_rows)

        self.assertEqual(merged[0]["company"], "Card Company")
        self.assertEqual(merged[0]["sector"], "Card Sector")
        self.assertEqual(merged[0]["current_price"], "$2,032.22")

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

    def test_monthly_updated_date_without_year_is_supported(self):
        # Current Quant GT Portfolio page drops the year/comma and glues the
        # MTD badge directly onto the day number with no separating space.
        text = "Latest holdings Updated August 1MTD +6.25% SYMBOL COMPANY ENTRY DATE PRICE"

        self.assertEqual(extract_pick_date(text, "monthly"), "Updated August 1")

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
