# QuantGT Detection Failure Ultracode Report

Date: 2026-07-02
Project: /opt/quantcheck

## Problem

The scheduled/manual QuantGT monitor failed after the QuantGT weekly page changed from the older `Weekly Picks` detail layout to a `Weekly Watchlist` card layout. The previous monitor expected weekly Top 10 rows with expandable detail fields and a tall page height. The new page still contains 10 valid symbols, but lacks the old GT score/detail structure and is shorter, so validation and screenshot readiness could fail.

Observed failing path:

- `python -m quantcheck.picks_check --test-email`
- Playwright timeout in `_wait_for_screenshot_ready()` while preparing weekly screenshot.
- Earlier parser/validator assumptions also rejected the new weekly watchlist layout.

## Root Cause

The monitor treated `/dashboard/weekly-picks` as one stable schema. QuantGT now renders a watchlist card schema:

- card text shape like `SNDK Sandisk Corporation $2,032.22 Electronic Technology`
- section label is `Watchlist`, not old weekly detail table text
- required fields are symbol/company/current price/sector, not GT score plus expanded detail panel fields
- page height can be close to one viewport, so the old height gate `height > window.innerHeight + 400` is too strict

## Fixes Applied

Files changed:

- `quantcheck/scrape_parse.py`
- `quantcheck/picks_report.py`
- `quantcheck/validation.py`
- `quantcheck/picks_check.py`
- `tests/test_scrape_parse.py`
- `tests/test_validation.py`

Behavior changes:

- Added parsing for weekly watchlist card rows.
- Added `source_kind=watchlist` row marker and weekly section `kind=watchlist`.
- Detect watchlist pages from path/title/body text.
- Skip old expanded-detail scraping for watchlist layout.
- Keep strict monthly validation unchanged.
- Validate watchlist rows against required watchlist fields: symbol, company, current_price, sector.
- Keep old fake/demo weekly rejection for non-watchlist weekly picks.
- Rename notifications from `Weekly Picks` to `Weekly Watchlist` when applicable.
- Support monthly date text `Updated on July 1, 2026`.
- Support `Analyst Consensus` as a detail label boundary/alias.
- Make the weekly screenshot height wait non-fatal after row parsing succeeds, because row readiness is the real hard gate.
- Added missing `--quiet` CLI argument used by baseline mode, so manual baseline recovery no longer crashes.

## Verification

Command:

```bash
cd /opt/quantcheck
.venv/bin/python -m unittest tests.test_scrape_parse tests.test_validation tests.test_scrape_parse_validation tests.test_fetch_resilience && .venv/bin/python -m quantcheck.picks_check --test-email
```

Result:

- 31 tests passed.
- Admin-only manual full-flow test passed.
- Test notification sent through admin route.
- Excel generated: `/opt/quantcheck/output/quantgt_picks_report_2026-07-02_131602.xlsx`
- Monthly screenshot generated: `/opt/quantcheck/screenshots/monthly_picks_2026-07-02_091602.png`
- Weekly screenshot generated: `/opt/quantcheck/screenshots/weekly_picks_2026-07-02_091602.png`
- Captured summary:
  - Monthly Picks: Updated on July 1, 2026, 5 stocks
  - Weekly Watchlist: Updated on Jun 26, 2026, 10 stocks
- Baseline recovery completed successfully after the fix.
- Health state reset to success:
  - `last_error`: null
  - `consecutive_failures`: 0
  - `mode`: baseline
- `quantcheck.service` restarted and is active.

## Safety Notes

- No ordinary subscriber preview/test was sent.
- No recipient files were edited.
- No outbound sender configuration was changed.
- No official inbound mail reader configuration was changed.
- Existing untracked backup/runtime files were left untouched.

## Residual Risk

If QuantGT changes the weekly card text order again, the watchlist regex may need another parser update. Current validation should fail closed if required watchlist fields disappear.
