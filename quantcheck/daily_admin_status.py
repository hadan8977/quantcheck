from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quantcheck.config import load_env
from quantcheck.email_templates import build_card_email_html
from quantcheck.gmail_api_notify import refresh_gmail_credentials, send_email as deliver_email
from quantcheck.notify_routes import EmailRoute, enforcement_enabled, recipients_for_route
from quantcheck.official_mail_forwarder import connect_imap
from quantcheck.service import ServiceError
from quantcheck.service import members as members_svc

ROOT = Path(os.environ.get("QUANTCHECK_HOME", Path(__file__).resolve().parents[1]))
STATE = ROOT / "state"
LOGS = ROOT / "logs"
LOG_FILE = LOGS / "daily_admin_status.log"
NY = ZoneInfo("America/New_York")


def log(message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    LOG_FILE.open("a", encoding="utf-8").write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": str(exc)}


def gmail_token_status(env: dict[str, str]) -> tuple[str, str]:
    raw_path = env.get("GMAIL_API_TOKEN") or env.get("OFFICIAL_MAIL_GMAIL_TOKEN") or ""
    if not raw_path:
        return "missing", "not configured"
    token_path = Path(raw_path)
    if not token_path.exists():
        return "missing", str(token_path)
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    try:
        creds = refresh_gmail_credentials(token_path, scopes)
        if creds.valid:
            return "valid", "Gmail API token refreshed/valid"
        return "invalid", f"expired={creds.expired} refresh_token={bool(creds.refresh_token)}"
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def imap_status(env: dict[str, str]) -> tuple[str, str]:
    missing = [
        key
        for key in ["OFFICIAL_MAIL_IMAP_HOST", "OFFICIAL_MAIL_IMAP_USERNAME", "OFFICIAL_MAIL_IMAP_PASSWORD"]
        if not env.get(key)
    ]
    if missing:
        return "missing", ", ".join(missing)
    try:
        client = connect_imap(env)
        try:
            client.logout()
        except Exception:
            pass
        return "valid", f"{env.get('OFFICIAL_MAIL_IMAP_USERNAME')} @ {env.get('OFFICIAL_MAIL_IMAP_HOST')}"
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def official_mail_reader_status(env: dict[str, str]) -> tuple[str, str, str]:
    provider = (env.get("OFFICIAL_MAIL_PROVIDER") or "gmail").strip().lower()
    if env.get("OFFICIAL_MAIL_ENABLED", "0") != "1":
        return "Official Mail Reader", "disabled", "OFFICIAL_MAIL_ENABLED is not 1"
    if provider == "imap":
        state, detail = imap_status(env)
        return "Official Mail IMAP", state, detail
    if provider == "gmail":
        state, detail = gmail_token_status(env)
        return "Official Mail Gmail API", state, detail
    return "Official Mail Reader", "error", f"unsupported provider={provider}"


def membership_cards(env: dict[str, str]) -> list[dict]:
    """Membership countdown section for the 2026-10-09 enforcement cliff:
    everyone migrated on 2026-08-31 expires at the same instant unless
    individually renewed first. This section exists so that gets watched
    every day between now and then, not discovered the hard way on 10-09.
    """
    enforcement = enforcement_enabled(env)
    enforcement_card = {
        "label": "Membership Enforcement",
        "value": "on" if enforcement else "OFF (MEMBERSHIP_ENFORCEMENT=0 kill switch active)",
        "tone": "neutral" if enforcement else "error",
    }
    try:
        report = members_svc.expiring_report(within_days=14, root=ROOT)
    except ServiceError as exc:
        return [enforcement_card, {"label": "Membership", "value": f"error reading state/memberships.json: {exc.message}", "tone": "error"}]

    now = datetime.fromisoformat(report["as_of"])
    cutoff_7d = now + timedelta(days=7)
    expiring_7d = [m for m in report["expiring"] if m["expires_at"] and datetime.fromisoformat(m["expires_at"]) <= cutoff_7d]
    expiring_list = ", ".join(f"{m['email']} ({m['expires_at']})" for m in report["expiring"]) or "none"

    return [
        enforcement_card,
        {"label": "Members Active", "value": report["active_count"]},
        {"label": "Members Expiring <=7d", "value": len(expiring_7d), "tone": "warning" if expiring_7d else "neutral"},
        {"label": "Members Expiring <=14d", "value": report["expiring_count"], "tone": "warning" if report["expiring_count"] else "neutral"},
        {"label": "Members Expired", "value": report["expired_count"], "tone": "error" if report["expired_count"] else "neutral"},
        {"label": "Expiring Within 14d", "value": expiring_list[:1500]},
    ]


def build_status(env: dict[str, str]) -> tuple[str, str, str]:
    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(NY)
    health = load_json(STATE / "health.json")
    official = load_json(STATE / "official_mail_forwarder_state.json")
    reader_label, reader_state, reader_detail = official_mail_reader_status(env)
    recipients = recipients_for_route(EmailRoute.ADMIN, env)

    cards = [
        {"label": "NY Time", "value": now_ny.isoformat()},
        {"label": "UTC Time", "value": now_utc.isoformat()},
        {"label": "Admin Recipients", "value": len(recipients)},
        {
            "label": reader_label,
            "value": f"{reader_state}: {reader_detail}",
            "tone": "error" if reader_state != "valid" else "neutral",
        },
        {
            "label": "Last Monitor Run",
            "value": health.get("last_run_at") or "missing",
            "tone": "warning" if not health.get("last_run_at") else "neutral",
        },
        {
            "label": "Last Monitor Success",
            "value": health.get("last_success_at") or "missing",
            "tone": "warning" if not health.get("last_success_at") else "neutral",
        },
        {
            "label": "Consecutive Failures",
            "value": health.get("consecutive_failures", 0),
            "tone": "error" if int(health.get("consecutive_failures") or 0) else "neutral",
        },
        {"label": "Last Window", "value": health.get("last_window") or "missing"},
        {
            "label": "Official Mail State",
            "value": json.dumps(official, ensure_ascii=True, sort_keys=True)[:1200] or "missing",
        },
        *membership_cards(env),
    ]
    subject = f"Quant GT Daily Admin Status - {now_ny.strftime('%Y-%m-%d')}"
    body = "\n".join(f"{card['label']}: {card['value']}" for card in cards)
    html = build_card_email_html("Daily Admin Status", cards, context="daily admin heartbeat")
    return subject, body, html


def send_daily_status() -> bool:
    env = load_env(ROOT, override=True)
    recipients = recipients_for_route(EmailRoute.ADMIN, env)
    if not recipients:
        log("skipped: no admin recipients configured")
        return False
    subject, body, html = build_status(env)
    ok = deliver_email(subject, body, to=recipients, html=html)
    log(("sent" if ok else "failed") + f": {subject} admin_count={len(recipients)}")
    print(subject)
    print(body)
    return ok


def main() -> None:
    raise SystemExit(0 if send_daily_status() else 1)


if __name__ == "__main__":
    main()