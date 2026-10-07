"""Operational tooling: run jobs, check status, diagnose problems, read logs.

See quantcheck/service/__init__.py for the module contract: JSON-serializable
dict returns, no printing, ServiceError on expected failure.

Deliberately NOT exposed here: the actual send path of
`quantcheck.historical_resend`. Only its dry-run preview is exposed
(`historical_resend_preview`). A real historical resend must go through
`python -m quantcheck.historical_resend --send --confirm-date ...` directly,
which has its own fail-closed confirmation gate; that gate is a documented
repo safety requirement and this layer does not get to shortcut it.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quantcheck import membership
from quantcheck import scheduler as scheduler_mod
from quantcheck import schedule as schedule_mod
from quantcheck import state as state_mod
from quantcheck.config import get_root, load_env
from quantcheck.historical_resend import ResendValidationError, prepare_resend
from quantcheck.service.errors import ServiceError
from quantcheck.service.members import _load_store  # reuse: same fail-loud-to-admin semantics as members.py reads

JOB_KINDS = ("picks", "health", "health_site", "official_mail", "daily_admin_status", "weekly_digest", "baseline", "screenshot", "test_email")
_ALWAYS_REQUIRES_CONFIRM = {"test_email"}

REPAIR_DOC_RELATIVE_PATH = "docs/SITE_CHANGE_REPAIR.md"

ALLOWED_LOG_NAMES = {
    "scheduler": "quantcheck_scheduler.log",
    "monitor": "quantgt_monitor.log",
    "health": "quantgt_health.log",
    "official_mail": "official_mail_forwarder.log",
    "email": "quantcheck_email.log",
    "daily_admin_status": "daily_admin_status.log",
    "notify_routes": "notify_routes.log",
}

# RFC 2606 reserved test domains: what every fixture/test email in this repo
# uses. recent_deliveries() must never present these as real deliveries.
FIXTURE_RECIPIENT_DOMAINS = {"example.com", "example.org", "example.net"}

_ERROR_LINE_PATTERN = re.compile(r"error|traceback|failed|exception", re.IGNORECASE)


def _context(root: Path | str | None) -> tuple[Path, dict]:
    resolved_root = Path(root) if root is not None else get_root()
    env = load_env(resolved_root, override=True)
    return resolved_root, env


def _lock_path(root: Path) -> Path:
    return root / "state" / "quantcheck.lock"


def _is_locked(lock_path: Path) -> bool:
    """Best-effort, non-destructive probe: true if some other process
    currently holds the exclusive lock. Opens with "a" (append) rather than
    "w" so a status check can never truncate the real lock file.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _dispatch(kind: str, *, force: bool, timeout: int | None) -> tuple[int, str]:
    """Run one job kind, funneled entirely through scheduler.run_cmd so this
    reuses the exact command shapes and subprocess semantics the daemon
    itself uses (see quantcheck/scheduler.py). Kinds picks/health/
    health_site/official_mail/daily_admin_status mirror scheduler.run_once's
    dispatch exactly; baseline/screenshot/test_email are additional
    on-demand modes picks_check.py supports that the daemon's own schedule
    never triggers.
    """
    python = sys.executable
    if kind == "picks":
        args = [python, "-m", "quantcheck.picks_check", "--mode", "check", "--no-random"]
        if force:
            args.append("--force")
        return scheduler_mod.run_cmd(args, timeout, capture_output=True)
    if kind == "baseline":
        args = [python, "-m", "quantcheck.picks_check", "--mode", "baseline", "--no-random", "--quiet"]
        if force:
            args.append("--force")
        return scheduler_mod.run_cmd(args, timeout, capture_output=True)
    if kind == "screenshot":
        args = [python, "-m", "quantcheck.picks_check", "--mode", "screenshot"]
        if force:
            args.append("--force")
        return scheduler_mod.run_cmd(args, timeout, capture_output=True)
    if kind == "health":
        return scheduler_mod.run_cmd([python, "-m", "quantcheck.health_watchdog"], timeout, capture_output=True)
    if kind == "health_site":
        rc1, out1 = scheduler_mod.run_cmd([python, "-m", "quantcheck.health_watchdog"], timeout, capture_output=True)
        rc2, out2 = scheduler_mod.run_cmd([python, "-m", "quantcheck.site_snapshot"], timeout, capture_output=True)
        rc3, out3 = 0, ""
        if rc2 == 0:
            rc3, out3 = scheduler_mod.run_cmd([python, "-m", "quantcheck.site_diff_notify"], timeout, capture_output=True)
        rc = max(rc1, rc2 if rc2 != 124 else 0, rc3)
        return rc, "\n".join(part for part in (out1, out2, out3) if part)
    if kind == "official_mail":
        rc, output = scheduler_mod.run_cmd([python, "-m", "quantcheck.official_mail_forwarder"], timeout, capture_output=True)
        if rc == 0:
            forwarded = scheduler_mod.official_mail_forwarded_count(output)
            if forwarded > 0:
                rc2, out2 = scheduler_mod.run_cmd(
                    [python, "-m", "quantcheck.picks_check", "--mode", "check", "--no-random", "--force"], timeout, capture_output=True
                )
                return (rc2 if rc2 else rc), output + "\n" + out2
        return rc, output
    if kind == "daily_admin_status":
        return scheduler_mod.run_cmd([python, "-m", "quantcheck.daily_admin_status"], timeout, capture_output=True)
    if kind == "weekly_digest":
        # Same fail-closed run the scheduler does: sends at most once per week_start.
        return scheduler_mod.run_cmd([python, "-m", "quantcheck.weekly_digest"], timeout, capture_output=True)
    if kind == "test_email":
        return scheduler_mod.run_cmd([python, "-m", "quantcheck.picks_check", "--test-email"], timeout, capture_output=True)
    raise ServiceError("invalid_job_kind", f"unknown job kind: {kind}", {"kind": kind, "valid_kinds": list(JOB_KINDS)})


def run_job(kind: str, force: bool = False, timeout: int | None = None, confirm: bool = False, *, root: Path | str | None = None) -> dict[str, Any]:
    """Run one job out of band from the scheduler.

    Reuses state/quantcheck.lock -- the same lock file the daemon
    (quantcheck/scheduler.py) holds for the duration of a scheduled run --
    so this can never race a real scheduled job. If the lock is held,
    returns {"skipped": "locked"} immediately rather than blocking.

    Confirmation gate: `test_email` always sends a real email, and `picks`
    with force=True *can* send one (force only bypasses the trading-window
    schedule gate in picks_check.run_check; it does not bypass the
    no-real-diff-no-notification rule or the duplicate-notification dedupe
    -- see quantcheck/picks_check.py:run_check). Both are treated as
    "may send real mail" and require confirm=True. Nothing else in
    JOB_KINDS requires it: official_mail, health*, daily_admin_status, and
    unforced picks are exactly the jobs the daemon already runs
    unattended, many times a day, with their own dedupe/no-op safeguards.
    """
    if kind not in JOB_KINDS:
        raise ServiceError("invalid_job_kind", f"unknown job kind: {kind}", {"kind": kind, "valid_kinds": list(JOB_KINDS)})

    requires_confirm = kind in _ALWAYS_REQUIRES_CONFIRM or (kind == "picks" and force)
    if requires_confirm and not confirm:
        raise ServiceError(
            "confirmation_required",
            f"kind={kind} force={force} can send real mail; retry with confirm=True",
            {"kind": kind, "force": force},
        )

    resolved_root, _env = _context(root)
    lock_path = _lock_path(resolved_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"kind": kind, "skipped": "locked"}
        try:
            returncode, output = _dispatch(kind, force=force, timeout=timeout)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    finished_at = datetime.now(timezone.utc)
    return {
        "kind": kind,
        "returncode": returncode,
        "ok": returncode == 0,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "output_tail": (output or "")[-2000:],
    }


def status(*, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    health = state_mod.load_json(resolved_root / "state" / "health.json", default={})
    latest = state_mod.load_json(resolved_root / "state" / "latest_picks.json", default={})
    previous = state_mod.load_json(resolved_root / "state" / "previous_picks.json", default={})
    official_mail_state = state_mod.load_json(resolved_root / "state" / "official_mail_forwarder_state.json", default={})

    lock_path = _lock_path(resolved_root)

    raw_schedule = env.get("QUANTCHECK_SCHEDULE") or None
    next_jobs: list[dict[str, Any]] = []
    try:
        # Several jobs can share one time slot; next_due_jobs returns all of
        # them in the order the daemon runs them (picks first).
        seconds, target, kinds = scheduler_mod.next_due_jobs(raw_schedule)
        next_jobs = [{"kind": kind, "at": target.isoformat(), "in_seconds": seconds} for kind in kinds]
        next_job: dict[str, Any] = next_jobs[0]  # backward compatible: the first job of the slot
    except Exception as exc:  # defensive: status() must never itself crash
        next_job = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "lock": {"path": str(lock_path), "held": _is_locked(lock_path)},
        "health": health or {},
        "latest_pick_dates": {
            "monthly": (latest.get("monthly") or {}).get("pick_date"),
            "weekly": (latest.get("weekly") or {}).get("pick_date"),
            "fetched_at": latest.get("fetched_at"),
        },
        "previous_pick_dates": {
            "monthly": (previous.get("monthly") or {}).get("pick_date"),
            "weekly": (previous.get("weekly") or {}).get("pick_date"),
            "fetched_at": previous.get("fetched_at"),
        },
        "official_mail_state": official_mail_state or {},
        "next_job": next_job,
        "next_jobs": next_jobs,
    }


def _finding(severity: str, check: str, message: str, *, doc_ref: str | None = None, **extra: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {"severity": severity, "check": check, "message": message}
    if doc_ref:
        finding["doc_ref"] = doc_ref
    finding.update(extra)
    return finding


def _tail_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _grep_recent_errors(path: Path, tail_lines: int = 500) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines[-tail_lines:] if _ERROR_LINE_PATTERN.search(line)]


def diagnose(*, root: Path | str | None = None) -> dict[str, Any]:
    """Machine-executable version of the Part A diagnostic checklist:
    scrape/parse health, snapshot freshness, recent delivery-ledger
    failures, official-mail dedupe state, and a log error scan. Each
    finding that maps to a documented failure mode references the matching
    docs/SITE_CHANGE_REPAIR.md section so a human or another agent can jump
    straight to the fix instead of re-deriving it.
    """
    resolved_root, env = _context(root)
    findings: list[dict[str, Any]] = []

    # 1. Scrape/parse health.
    health = state_mod.load_json(resolved_root / "state" / "health.json", default=None)
    if health is None:
        findings.append(
            _finding("error", "health_state", "state/health.json is missing or unreadable", doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A2: 五分钟诊断命令序列)")
        )
    else:
        failures = int(health.get("consecutive_failures") or 0)
        if failures >= 2:
            findings.append(
                _finding(
                    "error", "health_state", f"{failures} consecutive scrape failures",
                    doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A1: 分层定位表 - 抓取/解析)",
                    last_error=str(health.get("last_error") or "")[:500],
                )
            )
        elif failures == 1:
            findings.append(_finding("warning", "health_state", "1 consecutive scrape failure (not yet alerting)", doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A1: 分层定位表)"))
        else:
            findings.append(_finding("ok", "health_state", "no consecutive scrape failures"))

    # 2. Snapshot freshness.
    raw_dir = resolved_root / "state" / "raw"
    raw_files = sorted(raw_dir.glob("picks_raw_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if raw_dir.exists() else []
    if not raw_files:
        findings.append(_finding("warning", "snapshot_freshness", "no raw picks snapshots found under state/raw/", doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A2: 五分钟诊断命令序列)"))
    else:
        newest = raw_files[0]
        age_hours = (time.time() - newest.stat().st_mtime) / 3600
        severity = "warning" if age_hours > 30 else "ok"
        entry = _finding(severity, "snapshot_freshness", f"newest raw snapshot is {age_hours:.1f}h old ({newest.name})")
        if severity == "warning":
            entry["doc_ref"] = f"{REPAIR_DOC_RELATIVE_PATH} (A6: 修好抓取不等于自动补发)"
        findings.append(entry)

    # 3. Delivery ledger recent records.
    ledger_path = resolved_root / "logs" / "email_delivery_ledger.jsonl"
    if not ledger_path.exists():
        findings.append(_finding("warning", "delivery_ledger", "email_delivery_ledger.jsonl not found", doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A1: 分层定位表 - 通知/投递)"))
    else:
        recent = _tail_jsonl(ledger_path, 50)
        recent_failures = [r for r in recent if r.get("success") is False]
        if recent_failures:
            findings.append(
                _finding(
                    "error", "delivery_ledger", f"{len(recent_failures)} failed deliveries in the last {len(recent)} ledger records",
                    doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A1: 分层定位表 - 通知/投递)",
                    sample=recent_failures[:5],
                )
            )
        else:
            findings.append(_finding("ok", "delivery_ledger", f"no failed deliveries in the last {len(recent)} ledger records"))

    # 4. Official-mail dedupe state.
    official_state = state_mod.load_json(resolved_root / "state" / "official_mail_forwarder_state.json", default=None)
    if official_state is None:
        findings.append(
            _finding("warning", "official_mail_dedupe", "official_mail_forwarder_state.json is missing or unreadable", doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A1: 分层定位表 - 去重状态落盘时机)")
        )
    else:
        findings.append(_finding("ok", "official_mail_dedupe", "official_mail_forwarder_state.json parses"))

    # 5. Log error scan.
    log_findings = []
    for log_name in ("quantgt_monitor.log", "official_mail_forwarder.log", "quantcheck_scheduler.log"):
        error_lines = _grep_recent_errors(resolved_root / "logs" / log_name, tail_lines=500)
        if error_lines:
            log_findings.append({"log": log_name, "error_count": len(error_lines), "sample": error_lines[-3:]})
    if log_findings:
        findings.append(
            _finding(
                "warning", "log_error_scan", f"error-looking lines found in {len(log_findings)} log file(s)",
                doc_ref=f"{REPAIR_DOC_RELATIVE_PATH} (A2: 五分钟诊断命令序列)", logs=log_findings,
            )
        )
    else:
        findings.append(_finding("ok", "log_error_scan", "no error-looking lines in the last 500 lines of the scanned logs"))

    # 6. Membership store sanity (operationally important, outside Part A's scope).
    try:
        store = _load_store(resolved_root, env)
        findings.append(_finding("ok", "membership_store", f"{len(store.members)} member(s) on record"))
    except ServiceError as exc:
        findings.append(_finding("error", "membership_store", str(exc), doc_ref="docs/MEMBERSHIP.md"))

    # 7. Repair doc presence (Part A's own deliverable).
    if not (resolved_root / REPAIR_DOC_RELATIVE_PATH).exists():
        findings.append(_finding("warning", "repair_doc", f"{REPAIR_DOC_RELATIVE_PATH} not found"))

    severities = {f["severity"] for f in findings}
    overall = "error" if "error" in severities else ("warning" if "warning" in severities else "ok")
    return {"as_of": datetime.now(timezone.utc).isoformat(), "overall": overall, "findings": findings}


def logs(name: str, lines: int = 100, grep: str | None = None, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, _env = _context(root)
    filename = ALLOWED_LOG_NAMES.get(name)
    if filename is None:
        raise ServiceError("invalid_log_name", f"unknown log name: {name!r}", {"name": name, "valid_names": sorted(ALLOWED_LOG_NAMES)})
    path = resolved_root / "logs" / filename
    if not path.exists():
        return {"name": name, "path": str(path), "exists": False, "line_count": 0, "lines": []}
    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ServiceError("log_read_error", str(exc), {"path": str(path)}) from exc
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as exc:
            raise ServiceError("invalid_grep_pattern", str(exc), {"grep": grep}) from exc
        all_lines = [line for line in all_lines if pattern.search(line)]
    tail = all_lines[-lines:] if lines > 0 else []
    return {"name": name, "path": str(path), "exists": True, "line_count": len(tail), "lines": tail}


def _is_fixture_recipient(record: dict[str, Any]) -> bool:
    recipient = str(record.get("recipient") or record.get("to") or "").strip().lower()
    domain = recipient.rsplit("@", 1)[-1] if "@" in recipient else ""
    return domain in FIXTURE_RECIPIENT_DOMAINS


def recent_deliveries(limit: int = 50, *, root: Path | str | None = None) -> dict[str, Any]:
    """Reads logs/email_delivery_ledger.jsonl. Fixture recipients
    (@example.com/.org/.net -- the RFC 2606 reserved test domains every
    fixture and test in this repo uses) are filtered out so they are never
    mistaken for real deliveries; `filtered_fixture_count` reports how many
    were dropped for transparency.
    """
    resolved_root, _env = _context(root)
    ledger_path = resolved_root / "logs" / "email_delivery_ledger.jsonl"
    if not ledger_path.exists():
        return {"path": str(ledger_path), "count": 0, "filtered_fixture_count": 0, "deliveries": []}

    # Read a generous tail so filtering out fixture rows still leaves up to
    # `limit` real records when possible.
    raw_records = _tail_jsonl(ledger_path, max(limit * 4, 200))
    real_records = [r for r in raw_records if not _is_fixture_recipient(r)]
    trimmed = real_records[-limit:] if limit > 0 else []
    return {
        "path": str(ledger_path),
        "count": len(trimmed),
        "filtered_fixture_count": len(raw_records) - len(real_records),
        "deliveries": trimmed,
    }


def schedule_preview(days: int = 3, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    raw_schedule = env.get("QUANTCHECK_SCHEDULE") or None
    now_ny = datetime.now(membership.MEMBERSHIP_TZ)
    out_days = []
    for offset in range(max(days, 0)):
        day = (now_ny + timedelta(days=offset)).date()
        entries = schedule_mod.parse_schedule(raw_schedule, current_date=day)
        out_days.append(
            {
                "date": day.isoformat(),
                "is_trading_day": schedule_mod.is_trading_day(day),
                "jobs": [{"time": f"{h:02d}:{m:02d}", "kind": kind} for h, m, kind in entries],
            }
        )
    return {"as_of": now_ny.isoformat(), "timezone": "America/New_York", "days": out_days}


def historical_resend_preview(weekly_date: str, *, root: Path | str | None = None) -> dict[str, Any]:
    """Preview-only wrapper around quantcheck.historical_resend.prepare_resend.

    This deliberately does not call execute_resend. Sending a historical
    resend must go through `python -m quantcheck.historical_resend --send
    --confirm-date ...` directly; that CLI's fail-closed confirmation gate
    is a documented repo safety requirement this layer does not shortcut.
    """
    resolved_root, _env = _context(root)
    try:
        plan = prepare_resend(resolved_root, weekly_date)
    except ResendValidationError as exc:
        raise ServiceError("resend_validation_failed", str(exc), {"weekly_date": weekly_date}) from exc
    return plan.summary()
