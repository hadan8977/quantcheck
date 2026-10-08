# quantcheck

Standalone Quant GT monitor for Linux. It logs in to Quant GT, captures Monthly and Weekly Picks, detects real pick changes, and sends email alerts with Excel reports and screenshots.

It replaces a Hermes CronJob-style scheduler with a small self-contained Python daemon.

## Features

- Logged-in Quant GT scraping with Playwright.
- Monthly and Weekly Picks reports as Excel files.
- Optional forwarding of official Quant GT update emails from an IMAP inbox.
- Screenshot capture for changed pages.
- Diff logic that ignores market-noise fields such as price, return, market cap, and P/E.
- Guards against logged-out demo data and partial row-detail captures.
- Email delivery through Amazon SES API, SMTP, or Gmail API.
- Built-in New York time scheduler, file lock, timeouts, health checks, and logs.
- Site snapshots for Quant GT Picks, TradingView Indicator, AI Winners, RRG, Market Tools, and Study Guide pages.
- Membership-gated picks-update mail (9th-of-the-month billing anchor, fail-open by design) with a JSON CLI and an MCP server for agent-driven administration.

## Requirements

- Linux server.
- Python 3.11 or newer.
- systemd for production daemon deployment.
- Quant GT member credentials.
- Amazon SES API, SMTP credentials, or a Gmail API token for email alerts.

Windows is not a supported daemon runtime.

## Quickstart

```bash
git clone https://github.com/zhoucehuang-arch/quantcheck.git
cd quantcheck
bash scripts/install.sh
. .venv/bin/activate
```

Edit `.env`:

```env
QUANTGT_EMAIL=your_quantgt_email
QUANTGT_PASSWORD=your_quantgt_password
NOTIFY_EMAIL_TO=friend@example.com
NOTIFY_ADMIN_EMAIL_TO=admin@example.com

EMAIL_PROVIDER=brevo
BREVO_API_KEY=your_brevo_api_key
BREVO_FROM=Quant GT <notify@hadan.site>
```

Initialize baseline without sending a change alert:

```bash
python -m quantcheck.picks_check --mode baseline --force --no-random
```

Run checks manually:

```bash
quantcheck --once picks
quantcheck --once health_site
quantcheck --once official_mail
```

Run continuously:

```bash
bash scripts/run-daemon.sh
```

## Common Commands

```bash
# Send a full-flow test email with Excel and screenshots.
python -m quantcheck.picks_check --test-email

# Manage notification recipients safely.
quantcheck-recipients check
quantcheck-recipients add user@example.com
quantcheck-recipients add --role admin operator@example.com
quantcheck-recipients remove old@example.com --dry-run

# Run dependency-light local checks.
python -m compileall -q quantcheck tests
python -m unittest discover -s tests -v

# Membership and operations, JSON-first (agent-friendly; add --human for a table).
quantcheck-admin members list --status active
quantcheck-admin route preview
quantcheck-admin ops status
quantcheck-admin ops diagnose

# Install as a systemd service.
sudo rsync -a ./ /opt/quantcheck/
cd /opt/quantcheck
sudo bash scripts/install.sh
sudo cp systemd/quantcheck.service /etc/systemd/system/quantcheck.service
sudo systemctl daemon-reload
sudo systemctl enable --now quantcheck.service
```

## Runtime Files

- `state/`: latest/previous pick state, health state, raw audit captures, site snapshots, `memberships.json` (subscriber PII).
- `output/`: Excel reports.
- `screenshots/`: captured screenshots.
- `logs/`: scheduler, monitor, health, email, and `notify_routes.log` (membership filter decisions).

These paths are ignored by git.

## Documentation

- [Operations](docs/OPERATIONS.md): production deployment, schedule, credentials, logs, and troubleshooting.
- [Development](docs/DEVELOPMENT.md): project structure, tests, and maintenance notes.
- [Membership](docs/MEMBERSHIP.md): the 9th-of-the-month billing rule, storage format, fail-open guarantee, and the 2026-10-09 migration cliff.
- [Agent API](docs/AGENT_API.md): `quantcheck-admin` JSON CLI reference and the `quantcheck-mcp` MCP tool list.
- [Site Change Repair](docs/SITE_CHANGE_REPAIR.md): diagnostic checklist for "quantgt.io changed again" / "no email arrived."

## Safety Notes

- Never commit `.env`, `.config/`, `browser-profile/`, reports, screenshots, logs, raw state, or `state/memberships.json` (subscriber PII).
- Friends in `NOTIFY_EMAIL_TO` only receive successful picks-update reports; admin recipients receive all operator mail.
- Membership filtering fails OPEN: a broken/missing `state/memberships.json`, or a subscriber with no membership record, is always treated as a valid recipient rather than silently dropped. See [Membership](docs/MEMBERSHIP.md).
- Failed or partial scrapes must not overwrite `state/latest_picks.json`.
- No notification should be sent on no-change runs.
- `quantcheck-admin` / `quantcheck-mcp` never send subscriber mail without an explicit confirmation step, and neither exposes the ability to actually send a historical resend -- only its preview. See [Agent API](docs/AGENT_API.md).
