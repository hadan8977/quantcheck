"""quantcheck-admin: JSON-by-default CLI over quantcheck.service, meant for
agents and scripts. Pass --human for a readable rendering instead.

This is a thin dispatch layer only: every real behavior lives in
quantcheck.service.members / quantcheck.service.ops / notify_routes.route_preview.
Existing entry points (quantcheck, quantcheck-picks, quantcheck-report,
quantcheck-health, quantcheck-official-mail, quantcheck-recipients) are
untouched and remain the way to do everything this CLI does not cover.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from quantcheck.config import get_root, load_env
from quantcheck.notify_routes import EmailRoute, route_preview
from quantcheck.service import ServiceError
from quantcheck.service import members as members_svc
from quantcheck.service import ops as ops_svc

DEFAULT_MIGRATE_NOTE_TEMPLATE = "migrated from notify_recipients.txt on {date}"
DEFAULT_SET_EXPIRY_NOTE = "manual correction via quantcheck-admin"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _human_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


_TABLE_KEYS = ("members", "deliveries", "findings", "expiring", "jobs")


def _human_lines(data: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        table_keys = [
            key for key in _TABLE_KEYS
            if isinstance(data.get(key), list) and data[key] and all(isinstance(row, dict) for row in data[key])
        ]
        for key, value in data.items():
            if key in table_keys:
                continue
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(_human_lines(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_human_scalar(value)}")
        for key in table_keys:
            rows = data[key]
            lines.append(f"{pad}{key} ({len(rows)}):")
            columns = list(dict.fromkeys(col for row in rows for col in row if not isinstance(row.get(col), (dict, list))))
            for row in rows:
                summary = ", ".join(f"{col}={_human_scalar(row.get(col))}" for col in columns)
                lines.append(f"{pad}  - {summary}")
    elif isinstance(data, list):
        if not data:
            lines.append(f"{pad}(none)")
        for item in data:
            if isinstance(item, (dict, list)):
                lines.extend(_human_lines(item, indent))
            else:
                lines.append(f"{pad}- {_human_scalar(item)}")
    else:
        lines.append(f"{pad}{_human_scalar(data)}")
    return lines


def render(data: Any, human: bool) -> str:
    if not human:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return "\n".join(_human_lines(data))


# ---------------------------------------------------------------------------
# Command handlers -- each takes (args, root) and returns a JSON-serializable
# dict, or raises ServiceError. main() is the only place that catches.
# ---------------------------------------------------------------------------


def _cmd_members_list(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.list_members(status=args.status, expiring_within_days=args.expiring_days, root=root)


def _cmd_members_get(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.get_member(args.email, root=root)


def _cmd_members_add(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.add_member(args.email, args.months, note=args.note, joined_at=args.joined_at, root=root)


def _cmd_members_extend(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.extend_member(args.email, args.months, note=args.note, root=root)


def _cmd_members_set_expiry(args: argparse.Namespace, root: Path) -> dict:
    note = args.note if args.note is not None else DEFAULT_SET_EXPIRY_NOTE
    return members_svc.set_expiry(args.email, args.date, note, root=root)


def _cmd_members_remove(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.remove_member(args.email, args.reason, root=root)


def _cmd_members_migrate(args: argparse.Namespace, root: Path) -> dict:
    note = args.note if args.note is not None else DEFAULT_MIGRATE_NOTE_TEMPLATE.format(date=date.today().isoformat())
    return members_svc.migrate_from_recipients(args.expires, note, dry_run=args.dry_run, root=root)


def _cmd_members_sync(args: argparse.Namespace, root: Path) -> dict:
    return members_svc.sync_recipients(root=root)


def _cmd_route_preview(args: argparse.Namespace, root: Path) -> dict:
    env = load_env(root, override=True)
    return route_preview(args.route, env, root=root)


def _cmd_ops_status(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.status(root=root)


def _cmd_ops_run(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.run_job(args.kind, force=args.force, timeout=args.timeout, confirm=args.confirm, root=root)


def _cmd_ops_diagnose(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.diagnose(root=root)


def _cmd_ops_logs(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.logs(args.name, lines=args.lines, grep=args.grep, root=root)


def _cmd_ops_deliveries(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.recent_deliveries(limit=args.limit, root=root)


def _cmd_ops_schedule_preview(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.schedule_preview(days=args.days, root=root)


def _cmd_ops_resend_preview(args: argparse.Namespace, root: Path) -> dict:
    return ops_svc.historical_resend_preview(args.weekly_date, root=root)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantcheck-admin", description="JSON-first admin CLI for quantcheck membership and operations.")
    parser.add_argument("--root", type=Path, default=None, help="Quantcheck root directory. Defaults to QUANTCHECK_HOME or the project root.")
    parser.add_argument("--human", action="store_true", help="Print a human-readable rendering instead of JSON.")
    top = parser.add_subparsers(dest="command", required=True)

    members = top.add_parser("members", help="Membership CRUD, backed by state/memberships.json.")
    members_sub = members.add_subparsers(dest="members_command", required=True)

    p = members_sub.add_parser("list", help="List members, optionally filtered.")
    p.add_argument("--status", choices=members_svc.VALID_STATUS_FILTERS, default=None)
    p.add_argument("--expiring-days", type=int, default=None, dest="expiring_days")
    p.set_defaults(func=_cmd_members_list)

    p = members_sub.add_parser("get", help="Show one member's full record, including history.")
    p.add_argument("email")
    p.set_defaults(func=_cmd_members_get)

    p = members_sub.add_parser("add", help="Create a new member and add them to notify_recipients.txt.")
    p.add_argument("email")
    p.add_argument("--months", type=int, required=True)
    p.add_argument("--note", default=None)
    p.add_argument("--joined-at", default=None, dest="joined_at", help="YYYY-MM-DD or ISO datetime; defaults to now.")
    p.set_defaults(func=_cmd_members_add)

    p = members_sub.add_parser("extend", help="Add months to an existing member's expiry.")
    p.add_argument("email")
    p.add_argument("--months", type=int, required=True)
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_members_extend)

    p = members_sub.add_parser("set-expiry", help="Manually correct a member's expiry date (does not change status).")
    p.add_argument("email")
    p.add_argument("--date", default=None, help="YYYY-MM-DD or ISO datetime; omit or 'null' for never-expires (legacy).")
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_members_set_expiry)

    p = members_sub.add_parser("remove", help="Cancel a member and remove them from notify_recipients.txt.")
    p.add_argument("email")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_members_remove)

    p = members_sub.add_parser("migrate", help="One-time bulk migration from notify_recipients.txt. Idempotent.")
    p.add_argument("--expires", required=True, help="YYYY-MM-DD or ISO datetime; required (migration never creates never-expiring members).")
    p.add_argument("--note", default=None)
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.set_defaults(func=_cmd_members_migrate)

    p = members_sub.add_parser("sync", help="Report drift between memberships.json and notify_recipients.txt. Read-only.")
    p.set_defaults(func=_cmd_members_sync)

    route = top.add_parser("route", help="Preview who an email route currently reaches.")
    route_sub = route.add_subparsers(dest="route_command", required=True)
    p = route_sub.add_parser("preview")
    p.add_argument("--route", choices=[r.value for r in EmailRoute], default=EmailRoute.PICKS_UPDATE.value)
    p.set_defaults(func=_cmd_route_preview)

    ops = top.add_parser("ops", help="Operational status, jobs, diagnostics, logs.")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)

    p = ops_sub.add_parser("status", help="Daemon/job/lock/pick-date status.")
    p.set_defaults(func=_cmd_ops_status)

    p = ops_sub.add_parser("run", help="Run one job out of band. Reuses state/quantcheck.lock; never races the daemon.")
    p.add_argument("kind", choices=ops_svc.JOB_KINDS)
    p.add_argument("--force", action="store_true")
    p.add_argument("--confirm", action="store_true", help="Required for test_email and force=True picks (both can send real mail).")
    p.add_argument("--timeout", type=int, default=None)
    p.set_defaults(func=_cmd_ops_run)

    p = ops_sub.add_parser("diagnose", help="Machine-executable version of the Part A diagnostic checklist.")
    p.set_defaults(func=_cmd_ops_diagnose)

    p = ops_sub.add_parser("logs", help="Tail a known log file, optionally grepped.")
    p.add_argument("name", choices=sorted(ops_svc.ALLOWED_LOG_NAMES))
    p.add_argument("--lines", type=int, default=100)
    p.add_argument("--grep", default=None)
    p.set_defaults(func=_cmd_ops_logs)

    p = ops_sub.add_parser("deliveries", help="Recent email_delivery_ledger.jsonl records (fixture recipients filtered out).")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_ops_deliveries)

    p = ops_sub.add_parser("schedule-preview", help="Preview the next N days of scheduled jobs.")
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=_cmd_ops_schedule_preview)

    p = ops_sub.add_parser("resend-preview", help="Preview (never sends) a historical Weekly Watchlist resend.")
    p.add_argument("--weekly-date", required=True, dest="weekly_date")
    p.set_defaults(func=_cmd_ops_resend_preview)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root or get_root()

    func: Callable[[argparse.Namespace, Path], dict] = args.func
    try:
        result = func(args, root)
    except ServiceError as exc:
        print(render(exc.to_dict(), args.human))
        return 2
    except Exception as exc:  # last-resort safety net: agents parsing stdout must always get JSON, never a raw traceback
        print(render({"error": {"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}}, args.human))
        return 3

    print(render(result, args.human))
    return 0


if __name__ == "__main__":
    sys.exit(main())
