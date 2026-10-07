import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules.setdefault(
    "playwright.sync_api",
    types.SimpleNamespace(sync_playwright=lambda: None, TimeoutError=TimeoutError),
)

from openpyxl import load_workbook

from quantcheck import picks_excel
from quantcheck.diff import compare
from quantcheck.historical_resend import _validate_excel_symbols

OLD = {
    "fetched_at": "2026-09-30T21:51:44",
    "monthly": {"pick_date": "Updated September 1", "rows": [
        {"symbol": "HPE", "company": "Hewlett Packard Enterprise", "return": "+18.90%"},
        {"symbol": "DELL", "company": "Dell Technologies Inc.", "gt_score": "4.34/5"},
    ]},
    "weekly": {"pick_date": "Updated on Sep 25, 2026", "kind": "watchlist", "rows": [
        {"symbol": "TXG", "company": "10x Genomics, Inc.", "gt_score": "4.79/5"},
    ]},
}
NEW = {
    "fetched_at": "2026-10-01T12:30:00",
    "monthly": {"pick_date": "Updated October 1", "rows": [
        {"symbol": "MRNA", "company": "Moderna, Inc.", "held_since": "2026-10-01", "current_price": "$203.21",
         "buy_or_entry_price": "$186.61", "return": "+8.90%", "gt_score": "4.61/5", "analyst_signal": "Buy +0.42",
         "next_earnings": "Oct 29, 2026", "market_cap": "$81.10B", "revenue_growth_yoy": "-27.62%", "pe_ttm": "—"},
        {"symbol": "DELL", "company": "Dell Technologies Inc.", "gt_score": "3.99/5", "return": "weird value"},
    ]},
    "weekly": {"pick_date": "Updated on Sep 25, 2026", "kind": "watchlist", "rows": [
        {"symbol": "TXG", "company": "10x Genomics, Inc.", "gt_score": "4.79/5", "current_price": "$93.50"},
    ]},
}


class PicksExcelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "report.xlsx"

    def tearDown(self):
        self.tmp.cleanup()

    def _header_index(self, ws, header_row=picks_excel.HEADER_ROW):
        return {cell.value: cell.column for cell in ws[header_row]}

    def test_sheets_and_resend_symbol_contract(self):
        picks_excel.write_report(self.path, NEW, compare(OLD, NEW), OLD)
        wb = load_workbook(self.path)
        self.assertEqual(wb.sheetnames, ["Overview", "Changes", "Portfolio", "Weekly Watchlist"])
        _validate_excel_symbols(self.path, NEW)  # raises on mismatch

    def test_no_changes_sheet_without_diff(self):
        picks_excel.write_report(self.path, NEW)
        self.assertNotIn("Changes", load_workbook(self.path).sheetnames)

    def test_values_are_typed_with_number_formats(self):
        picks_excel.write_report(self.path, NEW, compare(OLD, NEW), OLD)
        ws = load_workbook(self.path)["Portfolio"]
        cols = self._header_index(ws)
        r = picks_excel.FIRST_DATA_ROW
        self.assertEqual(ws.cell(r, cols["Price"]).value, 203.21)
        self.assertEqual(ws.cell(r, cols["Price"]).number_format, picks_excel.FMT_MONEY)
        self.assertAlmostEqual(ws.cell(r, cols["Return"]).value, 0.089)
        self.assertEqual(ws.cell(r, cols["GT Score"]).value, 4.61)
        self.assertEqual(ws.cell(r, cols["Analyst Signal"]).value, "Buy")
        self.assertEqual(ws.cell(r, cols["Signal Score"]).value, 0.42)
        self.assertEqual(ws.cell(r, cols["Next Earnings"]).value, datetime(2026, 10, 29))
        self.assertEqual(ws.cell(r, cols["Market Cap"]).value, 81.10e9)
        self.assertNotIn("P/E (TTM)", cols)  # "—" for every row -> column dropped
        self.assertEqual(ws.cell(r, cols["Status"]).value, "NEW")
        # unparsable values stay visible as text instead of disappearing
        self.assertEqual(ws.cell(r + 1, cols["Return"]).value, "weird value")
        self.assertEqual(ws.cell(r + 1, cols["Status"]).value, "UPDATED")  # gt_score changed

    def test_all_empty_columns_are_dropped(self):
        picks_excel.write_report(self.path, NEW)
        ws = load_workbook(self.path)["Weekly Watchlist"]
        headers = set(self._header_index(ws))
        self.assertNotIn("Buy Price", headers)
        self.assertIn("Price", headers)

    def test_changes_sheet_lists_added_removed_and_field_updates(self):
        picks_excel.write_report(self.path, NEW, compare(OLD, NEW), OLD)
        ws = load_workbook(self.path)["Changes"]
        rows = [tuple(c.value for c in r) for r in ws.iter_rows(min_row=picks_excel.FIRST_DATA_ROW)]
        self.assertIn(("Portfolio", "Added", "MRNA", "Moderna, Inc.", None, None, None), rows)
        self.assertIn(("Portfolio", "Removed", "HPE", "Hewlett Packard Enterprise", None, None, None), rows)
        self.assertIn(("Portfolio", "Updated", "DELL", "Dell Technologies Inc.", "GT Score", "4.34/5", "3.99/5"), rows)


if __name__ == "__main__":
    unittest.main()
