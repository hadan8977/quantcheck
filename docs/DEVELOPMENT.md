# Development

This guide is for maintainers changing scraper behavior, diff rules, notification delivery, or the scheduler.

## Project Structure

```text
quantcheck/
  config.py              .env and path handling
  state.py               atomic JSON writes and retention pruning
  diff.py                pick diff logic and analyst-signal thresholds
  validation.py          member-data and demo-data guards
  schedule.py            daemon schedule parsing
  picks_report.py        Playwright scrape (Excel export delegates to picks_excel.py)
  picks_check.py         pick monitor orchestration
  picks_email.py         subscriber alert subject, HTML and plain-text body
  picks_excel.py         subscriber Excel report (typed cells, Changes sheet)
  picks_format.py        shared value parsing/labels for email and Excel
  weekly_digest.py       forwards the member-only Weekly Digest, once per week
  site_snapshot.py       authenticated site snapshot capture
  site_diff_notify.py    site-change diff and alerting
  official_mail_forwarder.py
                         IMAP detection and forwarding of official Quant GT mail
  health_watchdog.py     stale/failure health alerting
  gmail_api_notify.py    SMTP and Gmail API delivery
  recipients.py          safe CLI for subscriber/admin recipient files
scripts/
  install.sh             virtualenv install and Playwright browser install
  run-daemon.sh          local daemon launcher
systemd/
  quantcheck.service     production service unit
tests/
  _test_env.py           imported first by every test: points QUANTCHECK_HOME at a temp dir
  test_*.py              dependency-light unit tests for core logic
```

## Local Checks

Every test module starts with `import _test_env`, which points `QUANTCHECK_HOME` at a throwaway directory before any `quantcheck` import. On the server the repo root is the production install, so without it the suite wrote fake events into production logs and could read the production `.env`. Keep that import first in any new test file.

Run checks that do not need real Quant GT credentials:

```bash
python -m compileall -q quantcheck tests
python -m unittest discover -s tests -v
```

Run real-flow checks after changing selectors, login, screenshots, or notification behavior:

```bash
python -m quantcheck.picks_check --mode baseline --force --no-random
quantcheck --once picks
quantcheck --once health_site
quantcheck --once official_mail
python -m quantcheck.picks_check --test-email
```

## Design Notes

- `picks_report.py` owns Playwright scraping; `picks_excel.py` owns the Excel layout.
- `picks_email.py` owns the subscriber alert. Keep it pure (data, diff, previous snapshot in; strings out) and email-client safe: table layout, inline styles, no scheduler/window internals in subscriber mail. Admin-only notes go through the `banner` argument.
- To preview a design change without touching subscribers, render real `state/raw/` snapshots with `picks_email.build_html()` and send with `picks_check.send_email(..., route=EmailRoute.ADMIN)`.
- `historical_resend` validates the Excel by sheet name (`Portfolio`, `Weekly Watchlist`) and a `Symbol` header whose column holds only tickers; keep that contract when changing the Excel layout.
- `picks_check.py` orchestrates baseline/check/test-email flows.
- `official_mail_forwarder.py` forwards matching official Quant GT emails to the same picks-update route as scraper-detected changes.
- `diff.py` should stay pure and easy to unit test.
- `validation.py` rejects logged-out demo data and incomplete row-detail captures before state writes.
- `state.py` should be used for JSON state writes so interrupted runs do not corrupt files.
- `gmail_api_notify.py` should keep Gmail API permission limited to `gmail.modify` for inbox processing, and only use `gmail.send` for the legacy outbound path when explicitly enabled.

## Scraper Maintenance

When Quant GT changes its page structure:

- Update selectors in `picks_report.py`.
- Keep authentication checks in place before accepting captured data.
- Add or update unit tests for any pure parsing, diff, or validation rule.
- Run the real-flow checks with credentials before deploying.

## Release Checklist

```bash
python -m compileall -q quantcheck tests
python -m unittest discover -s tests -v
git status --short
```

For production changes, deploy to the server, run baseline/check/test-email once, then restart the systemd service.
