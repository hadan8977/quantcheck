from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quantcheck.config import load_env as load_dotenv
from quantcheck.schedule import parse_schedule

ROOT = Path(os.environ.get("QUANTCHECK_HOME", Path(__file__).resolve().parents[1]))
LOGS = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS / "quantcheck_scheduler.log"
LOCK_FILE = ROOT / "state" / "quantcheck.lock"
NY = ZoneInfo("America/New_York")

def log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{ts}] {msg}\n")


def load_env():
    load_dotenv(ROOT)


# When several jobs share a slot (e.g. month-end official_mail every 15 min
# lands on 08:30/09:00/17:00 picks), all of them run, in this order.
JOB_PRIORITY = ("picks", "health_site", "health", "daily_admin_status", "official_mail")


def next_due_jobs(raw_schedule: str | None = None, now: datetime | None = None):
    now = now or datetime.now(NY)
    candidates = []
    # Resolve dynamic default schedule separately for each candidate date so
    # a long-running daemon switches correctly between trading days and
    # weekends/market holidays without restart.
    for day_offset in range(0, 8):
        candidate_day = (now + timedelta(days=day_offset)).date()
        schedule = parse_schedule(raw_schedule, current_date=candidate_day)
        for h, m, kind in schedule:
            target = datetime.combine(candidate_day, datetime.min.time(), tzinfo=NY).replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                continue
            candidates.append((target, kind))
    if not candidates:
        # Defensive fallback; practically unreachable unless schedule parsing
        # returns no entries for a full week.
        tomorrow = (now + timedelta(days=1)).date()
        schedule = parse_schedule(raw_schedule, current_date=tomorrow)
        h, m, kind = schedule[0]
        target = datetime.combine(tomorrow, datetime.min.time(), tzinfo=NY).replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append((target, kind))
    target = min(t for t, _ in candidates)
    kinds = sorted({k for t, k in candidates if t == target}, key=lambda k: (JOB_PRIORITY.index(k) if k in JOB_PRIORITY else len(JOB_PRIORITY), k))
    return max(1, int((target - now).total_seconds())), target, kinds


def seconds_until_next(raw_schedule: str | None = None):
    sleep_s, target, kinds = next_due_jobs(raw_schedule)
    return sleep_s, target, kinds[0]


def run_cmd(args: list[str], timeout: int | None, *, capture_output: bool = False) -> int | tuple[int, str]:
    env = os.environ.copy()
    env.setdefault("QUANTCHECK_HOME", str(ROOT))
    start = time.time()
    try:
        p = subprocess.run(args, cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        output = p.stdout or ""
        if output:
            log(output[-8000:])
        log(f"command rc={p.returncode} elapsed={time.time()-start:.1f}s: {' '.join(args)}")
        if capture_output:
            return p.returncode, output
        return p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        if out:
            log(out[-4000:])
        log(f"timeout after {timeout}s: {' '.join(args)}")
        if capture_output:
            return 124, out
        return 124
    except Exception as e:
        log(f"command failed: {' '.join(args)} error={e}")
        if capture_output:
            return 1, ""
        return 1


def _optional_timeout(env_key: str) -> int | None:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def run_picks(force: bool = False):
    timeout = _optional_timeout("QUANTCHECK_SCAN_TIMEOUT_SECONDS")
    args = [sys.executable, "-m", "quantcheck.picks_check", "--mode", "check", "--no-random"]
    if force:
        args.append("--force")
    return run_cmd(args, timeout)


def run_health_site():
    # Health can self-alert by email. Site snapshot errors are logged and suppressed,
    # then health keeps monitoring stale-success failures.
    timeout = int(os.environ.get("QUANTCHECK_HEALTH_TIMEOUT_SECONDS", "110"))
    rc1 = run_cmd([sys.executable, "-m", "quantcheck.health_watchdog"], timeout)
    rc2 = run_cmd([sys.executable, "-m", "quantcheck.site_snapshot"], timeout)
    rc3 = 0
    if rc2 == 0:
        rc3 = run_cmd([sys.executable, "-m", "quantcheck.site_diff_notify"], timeout)
    return max(rc1, rc2 if rc2 != 124 else 0, rc3)


def official_mail_forwarded_count(output: str) -> int:
    start = output.rfind("{")
    if start < 0:
        return 0
    try:
        payload = json.loads(output[start:])
    except json.JSONDecodeError:
        return 0
    try:
        return int(payload.get("forwarded") or 0)
    except (TypeError, ValueError):
        return 0


def run_official_mail():
    timeout = int(os.environ.get("QUANTCHECK_MAIL_TIMEOUT_SECONDS", "60"))
    rc, output = run_cmd([sys.executable, "-m", "quantcheck.official_mail_forwarder"], timeout, capture_output=True)
    if rc != 0:
        return rc
    forwarded = official_mail_forwarded_count(output)
    if forwarded <= 0:
        return rc
    log(f"official mail forwarded={forwarded}; triggering forced picks check")
    picks_rc = run_picks(force=True)
    return picks_rc if picks_rc else rc


def run_daily_admin_status():
    timeout = int(os.environ.get("QUANTCHECK_MAIL_TIMEOUT_SECONDS", "60"))
    return run_cmd([sys.executable, "-m", "quantcheck.daily_admin_status"], timeout)


def run_once(kind: str):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"skip {kind}: another run is active")
            return 0
        jitter = int(os.environ.get("QUANTCHECK_JITTER_SECONDS", "5"))
        if jitter > 0:
            time.sleep(random.randint(0, jitter))
        if kind == "picks":
            return run_picks()
        if kind == "health_site":
            return run_health_site()
        if kind == "health":
            return run_cmd([sys.executable, "-m", "quantcheck.health_watchdog"], int(os.environ.get("QUANTCHECK_HEALTH_TIMEOUT_SECONDS", "110")))
        if kind == "official_mail":
            return run_official_mail()
        if kind == "daily_admin_status":
            return run_daily_admin_status()
        raise SystemExit(f"unknown job kind: {kind}")


def daemon():
    load_env()
    raw_schedule = os.environ.get("QUANTCHECK_SCHEDULE")
    stop = False
    def handler(signum, frame):
        nonlocal stop
        stop = True
        log(f"received signal {signum}; stopping")
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    log("scheduler started")
    while not stop:
        sleep_s, target, kinds = next_due_jobs(raw_schedule)
        log(f"next {'+'.join(kinds)} at {target.isoformat()} in {sleep_s}s")
        end = time.time() + sleep_s
        while not stop and time.time() < end:
            time.sleep(min(30, end - time.time()))
        if stop:
            break
        for kind in kinds:
            if stop:
                break
            run_once(kind)
    log("scheduler stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", choices=["picks", "health_site", "health", "official_mail"], help="run one job then exit")
    ap.add_argument("--daemon", action="store_true", help="run built-in scheduler loop")
    args = ap.parse_args()
    load_env()
    if args.once:
        raise SystemExit(run_once(args.once))
    daemon()


if __name__ == "__main__":
    main()
