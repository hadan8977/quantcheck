# Agent API: MCP server and JSON CLI

quantcheck exposes membership CRUD and operational tooling two ways, both
thin front ends over the same `quantcheck/service/` layer (never duplicated
logic):

- **`quantcheck-admin`**: a JSON-by-default CLI. Meant for scripts and
  agents that shell out; `--human` gives a readable rendering instead.
- **`quantcheck-mcp`**: an MCP stdio server. Meant for an agent (e.g.
  Claude Code) connected directly via the MCP protocol.

Neither one opens a network listener. There is no HTTP mode.

Every function in `quantcheck/service/members.py` and
`quantcheck/service/ops.py` returns a plain JSON-serializable dict, never
prints, never calls `sys.exit`, and raises `quantcheck.service.errors.ServiceError`
(`code`, `message`, `details`) for expected failures. Both front ends turn
that into the same shape:

```json
{"error": {"code": "member_not_found", "message": "no member found for x@y.com", "details": {"email": "x@y.com"}}}
```

## `quantcheck-admin` (JSON CLI)

```bash
quantcheck-admin [--root PATH] [--human] <command> ...
```

`--root` overrides the quantcheck installation directory (defaults to
`QUANTCHECK_HOME` or the repo root); mainly useful for testing against an
isolated directory. `--human` switches from JSON to an indented, readable
rendering -- useful at a terminal, not meant to be parsed.

Exit codes: `0` success, `2` a `ServiceError` (business-logic failure --
JSON error envelope on stdout), `3` an unexpected exception (same envelope
shape, `code: "internal_error"`). Malformed CLI usage itself (unknown
subcommand, invalid `--status` choice, missing required argument) is
argparse's own `SystemExit(2)` with a usage message on stderr -- standard
CLI behavior, not a `ServiceError`.

### Membership commands

```bash
quantcheck-admin members list [--status active|expired|cancelled|legacy] [--expiring-days N]
quantcheck-admin members get EMAIL
quantcheck-admin members add EMAIL (--months N | --expires-at DATE | --align) [--note "..."] [--joined-at 2026-08-10] [PAYMENT]
quantcheck-admin members extend EMAIL --months N [--note "..."] [PAYMENT]
quantcheck-admin members set-expiry EMAIL [--date 2026-12-09|null] [--note "..."]
quantcheck-admin members remove EMAIL --reason "..."
quantcheck-admin members migrate --expires 2026-10-09 [--note "..."] [--dry-run]
quantcheck-admin members sync

quantcheck-admin members bulk-add [EMAIL ...] [--file PATH|-] (--months N | --expires-at DATE | --align) [--note "..."] [PAYMENT] [--apply]
quantcheck-admin members bulk-extend [EMAIL ...] [--file PATH|-] [--all-active] --months N [--note "..."] [--apply]
quantcheck-admin members bulk-set-expiry [EMAIL ...] [--file PATH|-] [--all-active] --expires-at DATE [--note "..."] [--apply]
quantcheck-admin members record-payment EMAIL --amount X [--currency C] [--channel CH] [--paid-at DATE] [--ref R] [--note "..."]
quantcheck-admin members payments [--since DATE] [--until DATE]

# PAYMENT = [--amount X --currency C --channel CH --paid-at DATE --ref R]
```

`members add` takes **exactly one** of `--months`, `--expires-at` or
`--align`; this is validated in the service layer (`invalid_arguments`), not
by argparse, so the CLI and the MCP tool behave identically. `--months N`
counts from the next 9th anchor (so `--months 1` can be only a few days
long); `--expires-at` sets the expiry directly; `--align` copies the most
common expiry among currently-active members (`no_active_members` if there
are none). With `--expires-at`/`--align`, `months_total` stays `0` and the
history entry records `months: null` plus `"mode": "expires_at"|"align"`.

**Bulk commands are dry-run by default.** Without `--apply` they only report
what would happen (`"dry_run": true`) and write nothing; add `--apply` once
you have reviewed the per-email `results`. Emails come from positional
arguments and/or `--file PATH` (`-` reads stdin); the parser accepts
whitespace, commas, semicolons and newlines as separators and ignores
anything after a `#`, so a messy pasted list is fine. Invalid addresses
(`invalid_email`), existing members in `bulk-add` (`already_member`) and
unknown emails in `bulk-extend`/`bulk-set-expiry` (`member_not_found`) are
reported per email and skipped, never fatal; duplicates are collapsed.
`--all-active` (bulk-extend / bulk-set-expiry only; mutually exclusive with
listing emails) targets every currently-active member (not legacy, expired
or cancelled). An apply loads `memberships.json` once, saves it once and
writes `notify_recipients.txt` at most once. Per-member semantics are those
of `add` / `extend` (incl. reinstating a cancelled member) / `set-expiry`.

Bulk result shape: `{"dry_run", "counts": {"requested", "duplicates_ignored",
"ok", "skipped", "skipped_by_code"}, "results": [{"email", "outcome":
"ok"|"skipped", "code", "message", "action", "expires_at",
"previous_expires_at", ...}], "recipients_file", "store_backup"}`. A
`--amount` on `bulk-add` is recorded **per added member**, not as a total.

**Payments.** `add`/`extend`/`bulk-add` accept an optional payment: `amount`
(positive number, required once any payment field is given), `currency`
(upper-cased, e.g. `CNY`), `channel` (lower-cased, e.g. `wechat`/`alipay`),
`paid_at` (date or ISO datetime, default now), `ref`. It is stored inside the
history entry as `"payment": {...}`. `record-payment` appends a `"payment"`
history action **without** touching expiry, status, `months_total` or
`notify_recipients.txt`. `members get` shows derived `payments` and
`total_paid` (currency -> sum, missing currency under `"unspecified"`).
`members payments` returns `count`, `totals_by_currency`,
`totals_by_channel` (channel -> currency -> sum) and the list (`email`,
`paid_at`, `amount`, `currency`, `channel`, `ref`, `action`), filtered by
`paid_at` (`--since` inclusive; a bare-date `--until` includes that day).

`migrate` is the one-time bulk-migration tool (see `docs/MEMBERSHIP.md`);
it is idempotent, but there is normally no reason to run it again once the
81 pre-existing subscribers have been migrated -- `add_member` is the
right tool for anyone new.

### Route preview

```bash
quantcheck-admin route preview [--route picks_update|admin]
```

Shows exactly who a route reaches right now: `included`, `excluded` (with
`reason` and `expires_at` per excluded address), `enforcement` (on/off),
and `counts`. Read-only, does not write to any log. This is the safe way
to answer "would subscriber X get mail today" without sending anything.

### Ops commands

```bash
quantcheck-admin ops status
quantcheck-admin ops run KIND [--force] [--confirm] [--timeout SECONDS]
quantcheck-admin ops diagnose
quantcheck-admin ops logs NAME [--lines N] [--grep PATTERN]
quantcheck-admin ops deliveries [--limit N]
quantcheck-admin ops schedule-preview [--days N]
quantcheck-admin ops resend-preview --weekly-date "Updated on Aug 7, 2026"
```

`ops status` reports `next_jobs`: every job kind due at the next scheduler
slot, in execution order (several jobs can share a slot; `picks` runs
first). `next_job` is kept for backward compatibility and equals
`next_jobs[0]`.

`KIND` for `ops run` is one of `picks`, `health`, `health_site`,
`official_mail`, `daily_admin_status`, `baseline`, `screenshot`,
`test_email`. `ops run` reuses `state/quantcheck.lock` -- the exact lock
file the scheduler daemon holds for a scheduled run -- so it can never race
the daemon; if the lock is held, it returns `{"skipped": "locked"}`
immediately instead of blocking.

**`--confirm` is required** for `kind=test_email` (always sends a real
email) and for `kind=picks --force` (force only bypasses the trading-window
schedule gate, not the no-real-diff-no-notification rule or the
duplicate-notification dedupe in `picks_check.run_check`, so it *can* send
a real email). Every other kind runs without `--confirm` -- those are
exactly the jobs the daemon already runs unattended many times a day, each
with its own dedupe/no-op safeguards.

`ops logs NAME` reads from an allowlist (`scheduler`, `monitor`, `health`,
`official_mail`, `email`, `daily_admin_status`, `notify_routes`) -- there is
no way to read an arbitrary filesystem path through this command.

`ops deliveries` reads `logs/email_delivery_ledger.jsonl`, but filters out
any recipient on an RFC 2606 reserved test domain (`example.com`,
`example.org`, `example.net` -- what every fixture/test in this repo uses)
so a test run can never be mistaken for a real delivery; the response's
`filtered_fixture_count` reports how many were dropped.

`ops resend-preview` is a preview only -- see "What is deliberately not
exposed" below.

### Example: agent-friendly output

```bash
$ quantcheck-admin route preview
{
  "route": "picks_update",
  "enforcement": true,
  "included": ["...", "..."],
  "excluded": [],
  "counts": {"total": 83, "included": 82, "excluded": 0, "unknown_fail_open": 0, "subscribers_total": 81, "admins_total": 2}
}
```

## `quantcheck-mcp` (MCP stdio server)

### Registering with Claude Code

`/opt/quantcheck/.mcp.json`:

```json
{
  "mcpServers": {
    "quantcheck": {
      "command": "/opt/quantcheck/.venv/bin/quantcheck-mcp",
      "args": [],
      "env": {
        "QUANTCHECK_HOME": "/opt/quantcheck"
      }
    }
  }
}
```

Claude Code auto-discovers a `.mcp.json` in a project directory; running
Claude Code with `/opt/quantcheck` as (or under) the working directory picks
this up automatically. For a global/user-level registration instead, add
the same `quantcheck` entry under `mcpServers` in Claude Code's user MCP
config. Verify the connection with `claude mcp list` (from inside Claude
Code) or by checking that the 22 tools below appear in the connected
server's tool list.

Manual smoke test without Claude Code at all:

```bash
/opt/quantcheck/.venv/bin/quantcheck-mcp
# speaks MCP over stdio; Ctrl-C to exit. Use an MCP client (or
# mcp.client.stdio.stdio_client in a short Python script) to actually talk to it.
```

### Tool list (1:1 with `quantcheck/service/`)

None of these accept a `root` override -- there is exactly one production
deployment, and not exposing a raw filesystem path parameter to a
remote/LLM-driven caller is a deliberate reduction in attack surface.

| Tool | Maps to | Notes |
|---|---|---|
| `list_members` | `service.members.list_members` | `status`, `expiring_within_days` filters |
| `get_member` | `service.members.get_member` | full record incl. history |
| `add_member` | `service.members.add_member` | exactly one of `months` / `expires_at` / `align`; optional payment fields; also updates `notify_recipients.txt` |
| `extend_member` | `service.members.extend_member` | reinstates a cancelled member; optional payment fields |
| `set_expiry` | `service.members.set_expiry` | never changes `status` |
| `remove_member` | `service.members.remove_member` | also updates `notify_recipients.txt` |
| `expiring_report` | `service.members.expiring_report` | counts + soon-to-expire list |
| `sync_recipients` | `service.members.sync_recipients` | read-only drift report |
| `migrate_from_recipients` | `service.members.migrate_from_recipients` | **defaults to `dry_run=true`** |
| `bulk_add_members` | `service.members.bulk_add` | **`dry_run=true` by default**; `emails` is a list or a pasted string |
| `bulk_extend_members` | `service.members.bulk_extend` | **`dry_run=true` by default**; `emails` or `all_active=true` |
| `bulk_set_expiry` | `service.members.bulk_set_expiry` | **`dry_run=true` by default**; `emails` or `all_active=true` |
| `record_payment` | `service.members.record_payment` | history entry only; never changes expiry/status |
| `payments_report` | `service.members.payments_report` | totals by currency/channel + list, read-only |
| `route_preview` | `notify_routes.route_preview` | read-only, never logs |
| `ops_status` | `service.ops.status` | daemon/lock/pick-date status; `next_jobs` = all jobs in the next slot |
| `ops_run_job` | `service.ops.run_job` | needs `confirm=true` for `test_email` / forced `picks` |
| `ops_diagnose` | `service.ops.diagnose` | machine-executable Part-A checklist |
| `ops_logs` | `service.ops.logs` | allowlisted log names only |
| `ops_recent_deliveries` | `service.ops.recent_deliveries` | fixture recipients filtered |
| `ops_schedule_preview` | `service.ops.schedule_preview` | next N days of scheduled jobs |
| `ops_historical_resend_preview` | `service.ops.historical_resend_preview` | preview only, see below |

On the MCP side the payment is passed as flat fields (`amount`, `currency`,
`channel`, `paid_at`, `ref`) rather than a dict. The three bulk tools take
`dry_run: bool = True`: review the per-email `results` of the dry run, then
call again with `dry_run=false` to apply. The server `instructions` say the
same.

Every tool call returns clean, directly-`json.loads()`-able text content
and a correctly-set `isError` flag -- on a `ServiceError` the text is the
same `{"error": {...}}` envelope the CLI uses. This is deliberately *not*
implemented by raising an MCP `ToolError` from inside a tool: the `mcp` SDK
(2.x, `MCPServer`/`mcp.server.mcpserver`) prepends `"Error executing tool
<name>: "` to whatever a raised `ToolError` carries, which would break
naive JSON parsing on the caller side. Each tool instead directly returns
a `CallToolResult(..., is_error=True)` object, which the SDK passes through
unchanged.

The server's MCP `instructions` field (sent to the connecting client)
repeats the safety rules below in a form meant for a model to read
directly; see `quantcheck/mcp_server.py`'s `INSTRUCTIONS` constant.

## What is deliberately not exposed

- **No tool/command actually sends a historical resend.**
  `ops_historical_resend_preview` / `ops resend-preview` only preview what
  a resend of a given Weekly Watchlist date would contain (diff, rendered
  body, attachments). A real resend must go through
  `python -m quantcheck.historical_resend --send --confirm-date ...`
  directly on the server -- that CLI's own fail-closed `--confirm-date`
  gate is a documented repo safety requirement neither the service layer
  nor either front end gets to shortcut.
- **No tool/command can disable membership enforcement.**
  `MEMBERSHIP_ENFORCEMENT=0` is a `.env` kill switch, checked by
  `quantcheck.notify_routes` directly; there is no CLI command or MCP tool
  that flips it, on purpose.
- **No tool/command bypasses `ops run`'s confirmation gate.** There is no
  "force-confirm" or "skip lock" option.
