#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import email
import fcntl
import hashlib
import html as html_lib
import imaplib
import json
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses
from pathlib import Path
from typing import Iterable, List, Mapping

from quantcheck.config import load_env
from quantcheck.gmail_api_notify import refresh_gmail_credentials, send_email_per_recipient as deliver_email
from quantcheck.email_templates import build_card_email_html
from quantcheck.notify_routes import EmailRoute, recipients_for_route, route_label
from quantcheck.state import atomic_write_json

ROOT = Path(os.environ.get("QUANTCHECK_HOME", Path(__file__).resolve().parents[1]))
STATE = ROOT / "state"
LOGS = ROOT / "logs"
LOG_FILE = LOGS / "official_mail_forwarder.log"
STATE_FILE = STATE / "official_mail_forwarder_state.json"
# Separate from the scheduler's own state/quantcheck.lock: that lock is held by
# the parent scheduler process for the whole duration of a scheduled job,
# including while it runs this module as a subprocess, so reusing it here
# would make the subprocess dead-lock against its own parent. This lock only
# has to stop two *forwarder* runs (manual CLI + scheduled, or two manual
# runs) from racing each other and double-sending the same official email.
LOCK_FILE = STATE / "official_mail_forwarder.lock"

DEFAULT_SENDER_PATTERNS = ["@quantgt.io", "quant gt", "quantgt"]
DEFAULT_SUBJECT_PATTERNS = ["quant gt", "quantgt", "picks", "holdings", "portfolio", "daily admin status", "updated"]
DEFAULT_GMAIL_QUERY = "is:unread newer_than:14d (quantgt OR \"quant gt\" OR quantgt.io)"

STATE.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class OfficialMail:
    uid: str
    subject: str
    from_header: str
    date: str
    text: str
    html: str


def log(message: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")


def split_patterns(value: str | None, defaults: Iterable[str]) -> List[str]:
    raw = value if value is not None else ",".join(defaults)
    return [item.strip().lower() for item in re.split(r"[,;\n]+", raw or "") if item.strip()]


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            out.append(payload.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(payload)
    return "".join(out).strip()


def plain_text_from_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\s+", " ", text)).strip()


def extract_bodies(message: Message) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            text = raw if isinstance(raw, str) else ""
        else:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)
    html = "\n".join(html_parts).strip()
    text = "\n".join(plain_parts).strip()
    if not text and html:
        text = plain_text_from_html(html)
    return text, html


def official_mail_from_message(uid: str, raw_message: bytes) -> OfficialMail:
    msg = email.message_from_bytes(raw_message)
    text, html = extract_bodies(msg)
    return OfficialMail(
        uid=uid,
        subject=decode_header_value(msg.get("Subject")),
        from_header=decode_header_value(msg.get("From")),
        date=decode_header_value(msg.get("Date")),
        text=text,
        html=html,
    )


def _urlsafe_b64decode(data: str | None) -> str:
    if not data:
        return ""
    raw = data.encode("ascii")
    raw += b"=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")


def _gmail_headers(message: Mapping) -> dict[str, str]:
    headers = {}
    for item in (message.get("payload") or {}).get("headers") or []:
        name = str(item.get("name") or "").lower()
        if name:
            headers[name] = str(item.get("value") or "")
    return headers


def _walk_gmail_parts(payload: Mapping):
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk_gmail_parts(part)


def gmail_message_to_official_mail(message: Mapping) -> OfficialMail:
    payload = message.get("payload") or {}
    headers = _gmail_headers(message)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in _walk_gmail_parts(payload):
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            continue
        decoded = _urlsafe_b64decode(data)
        if mime_type == "text/html":
            html_parts.append(decoded)
        elif mime_type == "text/plain" or not mime_type:
            plain_parts.append(decoded)
    html = "\n".join(html_parts).strip()
    text = "\n".join(plain_parts).strip()
    if not text and html:
        text = plain_text_from_html(html)
    return OfficialMail(
        uid=str(message.get("id") or ""),
        subject=decode_header_value(headers.get("subject")),
        from_header=decode_header_value(headers.get("from")),
        date=decode_header_value(headers.get("date")),
        text=text,
        html=html,
    )


def gmail_messages_to_official_mails(messages: Iterable[Mapping]) -> list[OfficialMail]:
    return [gmail_message_to_official_mail(message) for message in messages]


def address_text(from_header: str) -> str:
    addresses = getaddresses([from_header])
    if not addresses:
        return from_header.lower()
    return " ".join(f"{name} {addr}" for name, addr in addresses).lower()


def matches_official_mail(mail: OfficialMail, sender_patterns: Iterable[str], subject_patterns: Iterable[str]) -> bool:
    # Manual mailbox forwarding can rewrite the visible From header, but we
    # only trust sender/header context, not arbitrary body mentions.
    header_context: list[str] = [address_text(mail.from_header)]
    body_header_match = re.search(r"^from:\s*(.+)$", mail.text or "", flags=re.IGNORECASE | re.MULTILINE)
    if body_header_match:
        header_context.append(body_header_match.group(1).strip())
    html_header_match = re.search(r"forwarded message.*?from:\s*([^<\n]+(?:<[^>]+>)?)", mail.html or "", flags=re.IGNORECASE | re.DOTALL)
    if html_header_match:
        header_context.append(html_header_match.group(1).strip())
    source_text = " ".join(header_context).lower()
    subject_text = " ".join([mail.subject, mail.text[:1000], mail.html[:1000]]).lower()
    sender_ok = any(pattern in source_text for pattern in sender_patterns)
    subject_ok = any(pattern in subject_text for pattern in subject_patterns)
    return sender_ok and subject_ok


def mail_fingerprint(mail: OfficialMail) -> str:
    payload = "\n".join([mail.from_header, mail.subject, mail.date, mail.text[:2000], mail.html[:2000]])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"forwarded": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"forwarded": []}


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["forwarded"] = list(state.get("forwarded") or [])[-500:]
    state["history"] = list(state.get("history") or [])[-200:]
    atomic_write_json(STATE_FILE, state)


def build_card_html(title: str, cards: list[Mapping[str, object]], context: str = "system") -> str:
    return build_card_email_html(title, cards, context=context)


def build_forward_body(mail: OfficialMail) -> tuple[str, str, str | None]:
    subject = f"Quant GT Official Email: {mail.subject or '(no subject)'}"
    body = "\n".join([
        "Forwarded official Quant GT email.",
        f"From: {mail.from_header}",
        f"Date: {mail.date or 'Unknown'}",
        f"Subject: {mail.subject or '(no subject)'}",
        "",
        mail.text or "(No plain-text body was available.)",
    ])
    html = None
    if mail.html:
        html = build_card_html(
            "Official Email",
            [
                {"label": "Status", "value": "Forwarded official Quant GT email."},
                {"label": "From", "value": mail.from_header},
                {"label": "Date", "value": mail.date or "Unknown"},
                {"label": "Subject", "value": mail.subject or "(no subject)"},
                {"label": "Original HTML", "value": "Included below"},
            ],
            context="official email redistributed",
        ).replace(
            "</table>",
            "</table><div style=\"border-top:1px solid #d7e3da;margin-top:8px;padding-top:14px;\">" + mail.html + "</div>",
        )
    return subject, body, html


def send_admin_alert(env: Mapping[str, str], subject: str, body: str) -> bool:
    recipients = recipients_for_route(EmailRoute.ADMIN, env)
    if not recipients:
        log(f"admin alert skipped: {route_label(EmailRoute.ADMIN)} not configured for {subject}")
        return False
    html = build_card_html(
        subject.replace("Quant GT ", ""),
        [
            {"label": "Route", "value": "administrators only"},
            {"label": "Message", "value": body, "tone": "error" if "Failed" in subject or "Failure" in subject else "warning"},
        ],
        context="official mail monitor alert",
    )
    delivered, failed = deliver_email(subject, body, to=recipients, html=html)
    if delivered:
        log(f"admin alert sent to {', '.join(delivered)}: {subject}")
    if failed:
        log(f"admin alert FAILED for {len(failed)} recipient(s): {', '.join(failed)}: {subject}")
    if not delivered and not failed:
        log(f"admin alert send failed: {subject}")
    return bool(delivered)


def alert_forward_failures(env: Mapping[str, str], result: Mapping[str, object]) -> bool:
    failed = int(result.get("failed") or 0)
    if failed <= 0:
        return False
    body = "\n".join([
        "Official Quant GT email was detected, but redistribution failed.",
        f"Checked: {result.get('checked', 0)}",
        f"Matched: {result.get('matched', 0)}",
        f"Forwarded: {result.get('forwarded', 0)}",
        f"Failed: {failed}",
        "",
        f"Log: {LOG_FILE}",
    ])
    return send_admin_alert(env, "Quant GT Official Mail Forward Failed", body)


def gmail_scopes(env: Mapping[str, str]) -> list[str]:
    raw = env.get("OFFICIAL_MAIL_GMAIL_SCOPES") or env.get("GMAIL_API_SCOPES")
    if raw:
        return [item.strip() for item in re.split(r"[,;\s]+", raw) if item.strip()]
    return ["https://www.googleapis.com/auth/gmail.modify"]


def gmail_service(env: Mapping[str, str]):
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except Exception as exc:
        raise RuntimeError("google client libraries are required for Gmail API official mail forwarding") from exc
    token_value = env.get("OFFICIAL_MAIL_GMAIL_TOKEN") or env.get("GMAIL_API_TOKEN")
    token_path = Path(token_value) if token_value else ROOT / ".config" / "gmail-api" / "token.json"
    if not token_path.is_absolute():
        token_path = ROOT / token_path
    if not token_path.exists():
        raise RuntimeError(f"missing Gmail API token: {token_path}")
    scopes = gmail_scopes(env)
    creds = refresh_gmail_credentials(token_path, scopes)
    if not creds.valid:
        raise RuntimeError("Gmail API credentials are not valid")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_gmail_messages(env: Mapping[str, str], query: str, max_messages: int) -> list[dict]:
    svc = gmail_service(env)
    response = svc.users().messages().list(userId="me", q=query, maxResults=max_messages).execute()
    out = []
    for item in response.get("messages") or []:
        msg = svc.users().messages().get(userId="me", id=item["id"], format="full").execute()
        out.append(msg)
    return out


def mark_gmail_message_read(env: Mapping[str, str], message_id: str) -> None:
    svc = gmail_service(env)
    svc.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()


def connect_imap(env: Mapping[str, str]):
    host = env.get("OFFICIAL_MAIL_IMAP_HOST") or env.get("IMAP_HOST")
    username = env.get("OFFICIAL_MAIL_IMAP_USERNAME") or env.get("IMAP_USERNAME")
    password = env.get("OFFICIAL_MAIL_IMAP_PASSWORD") or env.get("IMAP_PASSWORD")
    port = int(env.get("OFFICIAL_MAIL_IMAP_PORT") or env.get("IMAP_PORT") or "993")
    security = (env.get("OFFICIAL_MAIL_IMAP_SECURITY") or env.get("IMAP_SECURITY") or "ssl").strip().lower()
    if not (host and username and password):
        raise RuntimeError("missing OFFICIAL_MAIL_IMAP_HOST/OFFICIAL_MAIL_IMAP_USERNAME/OFFICIAL_MAIL_IMAP_PASSWORD")
    if security in {"ssl", "tls", "implicit_tls"}:
        client = imaplib.IMAP4_SSL(host, port)
    elif security in {"starttls", "start_tls"}:
        client = imaplib.IMAP4(host, port)
        client.starttls()
    elif security in {"plain", "none"}:
        client = imaplib.IMAP4(host, port)
    else:
        raise RuntimeError(f"unsupported OFFICIAL_MAIL_IMAP_SECURITY={security}")
    client.login(username, password)
    return client


def search_uids(client, mailbox: str, query: str) -> list[str]:
    status, _ = client.select(mailbox, readonly=True)
    if status != "OK":
        raise RuntimeError(f"cannot select mailbox: {mailbox}")
    status, data = client.uid("search", None, query)
    if status != "OK":
        raise RuntimeError(f"imap search failed: {query}")
    return [uid.decode("ascii") for uid in (data[0] or b"").split()]


def fetch_message(client, uid: str) -> bytes:
    status, data = client.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data:
        raise RuntimeError(f"imap fetch failed for uid {uid}")
    for item in data:
        if isinstance(item, tuple) and item[1]:
            return item[1]
    raise RuntimeError(f"imap fetch returned no message body for uid {uid}")


def search_query(env: Mapping[str, str]) -> str:
    return env.get("OFFICIAL_MAIL_IMAP_SEARCH") or 'FROM "quantgt.io"'


def _record_forward(state: dict, forwarded: set[str], fingerprint: str, mail: OfficialMail, provider: str) -> None:
    forwarded.add(fingerprint)
    state.setdefault("history", []).append({
        "uid": mail.uid,
        "subject": mail.subject,
        "from": mail.from_header,
        "date": mail.date,
        "provider": provider,
        "fingerprint": fingerprint,
        "forwarded_at": datetime.now(timezone.utc).isoformat(),
    })


def _forward_mails(mails: Iterable[OfficialMail], env: Mapping[str, str], recipients: list[str], sender_patterns: list[str], subject_patterns: list[str], state: dict, forwarded: set[str], result: dict, *, provider: str, dry_run: bool) -> None:
    for mail in mails:
        result["checked"] += 1
        if not matches_official_mail(mail, sender_patterns, subject_patterns):
            continue
        result["matched"] += 1
        fingerprint = mail_fingerprint(mail)
        if fingerprint in forwarded:
            continue
        subject, body, html = build_forward_body(mail)
        if not dry_run:
            delivered, failed = deliver_email(subject, body, to=recipients, html=html)
            if failed:
                result["failed"] += 1
                log(f"official mail delivery FAILED for {len(failed)} recipient(s) provider={provider} uid={mail.uid}: {', '.join(failed)} subject={mail.subject!r}")
            if failed or not delivered:
                if not delivered:
                    log(f"official mail send failed provider={provider} uid={mail.uid} subject={mail.subject!r}")
                continue
            _record_forward(state, forwarded, fingerprint, mail, provider)
            # Persist the dedupe fingerprint immediately, not just once at the
            # end of the run. A run that sends several mails, then gets
            # interrupted (service restart, crash) or overlaps with another
            # invocation before reaching its own final save, must not leave
            # an already-sent mail looking unforwarded to the next run.
            state["forwarded"] = sorted(forwarded)
            save_state(state)
        result["forwarded"] += 1
        result.setdefault("_forwarded_uids", []).append(mail.uid)
        log(f"forwarded official mail provider={provider} uid={mail.uid} subject={mail.subject!r} to={', '.join(recipients)}")


def _forward_from_imap(env: Mapping[str, str], recipients: list[str], sender_patterns: list[str], subject_patterns: list[str], state: dict, forwarded: set[str], result: dict, *, dry_run: bool) -> None:
    mailbox = env.get("OFFICIAL_MAIL_IMAP_MAILBOX") or "INBOX"
    max_messages = int(env.get("OFFICIAL_MAIL_MAX_MESSAGES") or "20")
    client = connect_imap(env)
    try:
        uids = search_uids(client, mailbox, search_query(env))[-max_messages:]
        mails = [official_mail_from_message(uid, fetch_message(client, uid)) for uid in uids]
        _forward_mails(mails, env, recipients, sender_patterns, subject_patterns, state, forwarded, result, provider="imap", dry_run=dry_run)
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _forward_from_gmail(env: Mapping[str, str], recipients: list[str], sender_patterns: list[str], subject_patterns: list[str], state: dict, forwarded: set[str], result: dict, *, dry_run: bool) -> None:
    query = env.get("OFFICIAL_MAIL_GMAIL_QUERY") or DEFAULT_GMAIL_QUERY
    max_messages = int(env.get("OFFICIAL_MAIL_MAX_MESSAGES") or "20")
    messages = list_gmail_messages(env, query, max_messages)
    mails = gmail_messages_to_official_mails(messages)
    _forward_mails(mails, env, recipients, sender_patterns, subject_patterns, state, forwarded, result, provider="gmail", dry_run=dry_run)
    if not dry_run:
        for message_id in result.get("_forwarded_uids", []):
            mark_gmail_message_read(env, message_id)


def forward_official_mail(env: Mapping[str, str], *, dry_run: bool = False, admin_only: bool = False) -> dict:
    if env.get("OFFICIAL_MAIL_ENABLED", "0") != "1":
        log("official mail forward skipped: OFFICIAL_MAIL_ENABLED is not 1")
        return {"checked": 0, "matched": 0, "forwarded": 0, "skipped": "disabled"}

    route = EmailRoute.ADMIN if admin_only else EmailRoute.PICKS_UPDATE
    recipients = recipients_for_route(route, env)
    if not recipients:
        log(f"mail forward skipped: {route_label(route)} not configured")
        return {"checked": 0, "matched": 0, "forwarded": 0, "skipped": "no_recipients"}

    sender_patterns = split_patterns(env.get("OFFICIAL_MAIL_SENDER_PATTERNS"), DEFAULT_SENDER_PATTERNS)
    subject_patterns = split_patterns(env.get("OFFICIAL_MAIL_SUBJECT_PATTERNS"), DEFAULT_SUBJECT_PATTERNS)
    provider = (env.get("OFFICIAL_MAIL_PROVIDER") or "gmail").strip().lower()
    state = load_state()
    forwarded = set(state.get("forwarded") or [])
    result = {"checked": 0, "matched": 0, "forwarded": 0, "failed": 0, "dry_run": dry_run, "provider": provider, "admin_only": admin_only}

    if provider == "imap":
        _forward_from_imap(env, recipients, sender_patterns, subject_patterns, state, forwarded, result, dry_run=dry_run)
    elif provider == "gmail":
        _forward_from_gmail(env, recipients, sender_patterns, subject_patterns, state, forwarded, result, dry_run=dry_run)
    else:
        raise RuntimeError(f"unsupported OFFICIAL_MAIL_PROVIDER: {provider}")

    state["forwarded"] = sorted(forwarded)
    if not dry_run and result["forwarded"] > 0:
        save_state(state)
    result.pop("_forwarded_uids", None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="check and match messages without sending")
    args = parser.parse_args()
    load_env(ROOT)
    env = dict(os.environ)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("official mail forward skipped: another forwarder run holds the lock")
            print(json.dumps({"checked": 0, "matched": 0, "forwarded": 0, "skipped": "locked"}, ensure_ascii=False, indent=2))
            return
        try:
            result = forward_official_mail(env, dry_run=args.dry_run)
            if not args.dry_run:
                alert_forward_failures(env, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            tb = traceback.format_exc()
            log(f"official mail forward failed: {exc}\n{tb}")
            if env.get("OFFICIAL_MAIL_ENABLED", "0") == "1":
                send_admin_alert(
                    env,
                    "Quant GT Official Mail Check Failed",
                    f"Error: {exc}\n\n{tb[-3000:]}",
                )
            raise


if __name__ == "__main__":
    main()
