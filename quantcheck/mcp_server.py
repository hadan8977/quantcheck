"""quantcheck-mcp: MCP stdio server exposing quantcheck.service as tools.

Every tool here is a thin wrapper around exactly one quantcheck.service.members
or quantcheck.service.ops function (plus notify_routes.route_preview), 1:1.
None of them accept a `root` override -- there is one production deployment,
and not exposing a raw filesystem path parameter to a remote/LLM-driven
caller is a deliberate reduction in attack surface.

Error contract: every tool always returns a CallToolResult with clean,
directly-`json.loads`-able text content, never a Python traceback. On a
ServiceError (an anticipated failure -- bad input, missing record, storage
trouble) the result has is_error=True and the text is
`{"error": {"code", "message", "details"}}`, exactly like quantcheck-admin's
JSON-on-stdout error envelope. On any other exception, the same shape is
used with code "internal_error" -- this deliberately returns a CallToolResult
object directly rather than raising, because raising ToolError here would
get the framework's own "Error executing tool <name>: " prefix glued onto
the front of the JSON, breaking naive json.loads() on the caller side.

Nothing here can send real subscriber mail without an explicit confirm=True
(see ops_run_job), and the actual historical-resend *send* path is not
exposed at all -- only its preview (ops_historical_resend_preview). See
quantcheck/service/ops.py's module docstring for why.
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from quantcheck.notify_routes import EmailRoute
from quantcheck.notify_routes import route_preview as _route_preview
from quantcheck.service import ServiceError
from quantcheck.service import members as members_svc
from quantcheck.service import ops as ops_svc

INSTRUCTIONS = """\
quantcheck operates a paid 2C subscriber list for Quant GT pick alerts.

Safety rules that apply to every tool call here:
- Membership filtering fails OPEN: a broken/missing membership store, or a \
subscriber address with no membership record, is always treated as a valid \
recipient rather than silently dropped. This is enforced in \
quantcheck.notify_routes, not in this server -- it is not something a tool \
call here can override.
- MEMBERSHIP_ENFORCEMENT=0 (a .env kill switch, not an MCP tool) disables \
membership filtering entirely; there is no tool here for that on purpose.
- ops_run_job requires confirm=true for `test_email` and for `picks` with \
force=true, because both of those can send a real email. Nothing else \
requires it (official_mail, health*, daily_admin_status, and unforced picks \
are exactly what the daemon already runs unattended many times a day, each \
with its own dedupe/no-op safeguards).
- migrate_from_recipients defaults to dry_run=true. Only pass dry_run=false \
once you have reviewed the dry-run output.
- There is no tool to actually send a historical resend -- \
ops_historical_resend_preview only previews one. A real resend must go \
through `python -m quantcheck.historical_resend --send --confirm-date ...` \
directly on the server, which has its own fail-closed confirmation gate.
"""

server = MCPServer(name="quantcheck", instructions=INSTRUCTIONS)


def _ok(data: Any) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2, default=str))], is_error=False)


def _err(payload: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))], is_error=True)


def _call(fn, /, *args: Any, **kwargs: Any) -> CallToolResult:
    try:
        data = fn(*args, **kwargs)
    except ServiceError as exc:
        return _err(exc.to_dict())
    except Exception as exc:  # last-resort: never let a raw traceback reach the caller
        return _err({"error": {"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}})
    return _ok(data)


# ---------------------------------------------------------------------------
# Membership tools (quantcheck.service.members, 1:1)
# ---------------------------------------------------------------------------


@server.tool()
def list_members(status: str | None = None, expiring_within_days: int | None = None) -> CallToolResult:
    """List members from state/memberships.json.

    status: filter to exactly one of "active", "expired", "cancelled", "legacy" (omit for all).
    expiring_within_days: only include active members whose expiry falls within this many days from now.
    """
    return _call(members_svc.list_members, status=status, expiring_within_days=expiring_within_days)


@server.tool()
def get_member(email: str) -> CallToolResult:
    """Show one member's full record, including their history of add/extend/remove/migrate actions."""
    return _call(members_svc.get_member, email)


@server.tool()
def add_member(email: str, months: int, note: str | None = None, joined_at: str | None = None) -> CallToolResult:
    """Create a NEW member and add them to notify_recipients.txt so they start receiving picks mail.

    Fails with member_already_exists if this email is already a member -- use extend_member instead.
    months: how many months of membership to grant, starting from the next 9th-of-the-month anchor after joined_at.
    joined_at: "YYYY-MM-DD" or a full ISO datetime; defaults to now if omitted.
    """
    return _call(members_svc.add_member, email, months, note=note, joined_at=joined_at)


@server.tool()
def extend_member(email: str, months: int, note: str | None = None) -> CallToolResult:
    """Add months to an existing member's expiry (stacks on top of their current expiry if still active,
    otherwise restarts from now). Also reinstates a cancelled member back to active -- paying again is
    treated as an unambiguous signal they should be receiving mail again.
    """
    return _call(members_svc.extend_member, email, months, note=note)


@server.tool()
def set_expiry(email: str, expires_at: str | None = None, note: str = "manual correction via MCP") -> CallToolResult:
    """Manually correct a member's expiry date. Pass expires_at=null (or omit it) for never-expires (legacy).

    This is a pure date-correction tool: unlike extend_member, it deliberately does NOT change a
    cancelled member's status back to active. Use extend_member for that.
    """
    return _call(members_svc.set_expiry, email, expires_at, note)


@server.tool()
def remove_member(email: str, reason: str) -> CallToolResult:
    """Cancel a member (status becomes "cancelled", excluded from mail going forward) and remove them
    from notify_recipients.txt. Their record and history are preserved, not deleted.
    """
    return _call(members_svc.remove_member, email, reason)


@server.tool()
def expiring_report(within_days: int = 14) -> CallToolResult:
    """Membership counts (active/expiring/expired/cancelled/legacy) and the list of members expiring
    within `within_days` days, soonest first. Read-only.
    """
    return _call(members_svc.expiring_report, within_days=within_days)


@server.tool()
def sync_recipients() -> CallToolResult:
    """Reconcile state/memberships.json against notify_recipients.txt and report drift between them.
    Read-only -- reports only, never modifies either file.
    """
    return _call(members_svc.sync_recipients)


@server.tool()
def migrate_from_recipients(expires_at: str, note: str | None = None, dry_run: bool = True) -> CallToolResult:
    """One-time bulk migration: every address in notify_recipients.txt without a membership record yet
    gets one, with the given expires_at/note. Idempotent -- already-migrated emails are never re-added
    or overwritten, so re-running after manual corrections is always safe.

    Defaults to dry_run=true. Only pass dry_run=false after reviewing the dry-run output; this is the
    one membership-CRUD tool that can touch dozens of real records at once.
    """
    return _call(members_svc.migrate_from_recipients, expires_at, note, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Routing preview (quantcheck.notify_routes.route_preview)
# ---------------------------------------------------------------------------


@server.tool()
def route_preview(route: str = "picks_update") -> CallToolResult:
    """Preview exactly who a mail route currently reaches, without sending anything.

    route: "picks_update" (subscribers + admins, membership-filtered) or "admin" (admins only, never filtered).
    Returns included/excluded lists, enforcement on/off, and counts. This is the safe way to check
    membership-filter state; it never writes to notify_routes.log the way an actual send does.
    """
    try:
        data = _route_preview(EmailRoute(route), dict(os.environ))
    except ValueError as exc:
        return _err({"error": {"code": "invalid_route", "message": str(exc), "details": {"route": route}}})
    return _ok(data)


# ---------------------------------------------------------------------------
# Ops tools (quantcheck.service.ops, 1:1)
# ---------------------------------------------------------------------------


@server.tool()
def ops_status() -> CallToolResult:
    """Daemon/job status: lock state, health.json contents, latest/previous pick dates, and the next
    scheduled job. Read-only.
    """
    return _call(ops_svc.status)


@server.tool()
def ops_run_job(kind: str, force: bool = False, timeout: int | None = None, confirm: bool = False) -> CallToolResult:
    """Run one job out of band from the scheduler, right now.

    kind: one of "picks", "health", "health_site", "official_mail", "daily_admin_status", "baseline",
    "screenshot", "test_email".

    Reuses state/quantcheck.lock (the same lock the daemon holds) so this can never race a scheduled
    run; if the lock is held, returns {"skipped": "locked"} immediately instead of blocking.

    confirm=true is REQUIRED for kind="test_email" (always sends a real email) and for kind="picks"
    with force=true (force only bypasses the trading-window schedule gate, not the no-real-diff-no-
    notification rule or the duplicate-notification dedupe, so it *can* send a real email). Every other
    kind runs without confirm -- those are exactly the jobs the daemon already runs unattended many
    times a day, each with its own dedupe/no-op safeguards.
    """
    return _call(ops_svc.run_job, kind, force=force, timeout=timeout, confirm=confirm)


@server.tool()
def ops_diagnose() -> CallToolResult:
    """Machine-executable version of the site-change diagnostic checklist (docs/SITE_CHANGE_REPAIR.md):
    scrape/parse health, snapshot freshness, recent delivery-ledger failures, official-mail dedupe
    state, and a log error scan. Returns a list of findings with severity (ok/warning/error) and, where
    relevant, a doc_ref pointing at the matching section of that document. Read-only.
    """
    return _call(ops_svc.diagnose)


@server.tool()
def ops_logs(name: str, lines: int = 100, grep: str | None = None) -> CallToolResult:
    """Tail a known log file, optionally filtered by a regex.

    name: one of "scheduler", "monitor", "health", "official_mail", "email", "daily_admin_status",
    "notify_routes". This is an allowlist -- there is no way to read an arbitrary path.
    """
    return _call(ops_svc.logs, name, lines=lines, grep=grep)


@server.tool()
def ops_recent_deliveries(limit: int = 50) -> CallToolResult:
    """Recent records from logs/email_delivery_ledger.jsonl. Fixture recipients (@example.com/.org/.net,
    the reserved test domains every test in this repo uses) are filtered out and never presented as
    real deliveries; filtered_fixture_count reports how many were dropped. Read-only.
    """
    return _call(ops_svc.recent_deliveries, limit=limit)


@server.tool()
def ops_schedule_preview(days: int = 3) -> CallToolResult:
    """Preview the next N days of scheduled jobs (trading-day vs non-trading-day schedule, and the
    month-end official-mail polling window), all times America/New_York. Read-only.
    """
    return _call(ops_svc.schedule_preview, days=days)


@server.tool()
def ops_historical_resend_preview(weekly_date: str) -> CallToolResult:
    """Preview (never sends) what a historical Weekly Watchlist resend for `weekly_date` would contain --
    the diff, attachments, and rendered body. weekly_date must exactly match the internal date string,
    e.g. "Updated on Aug 7, 2026". There is no tool to actually send this; see this server's own
    instructions for why.
    """
    return _call(ops_svc.historical_resend_preview, weekly_date)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
