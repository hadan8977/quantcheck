from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Mapping

from quantcheck.gmail_api_notify import parse_recipients
from quantcheck import membership
from quantcheck import membership_store

ROOT = Path(os.environ.get("QUANTCHECK_HOME", Path(__file__).resolve().parents[1]))
LOGS = ROOT / "logs"
LOG_FILE = LOGS / "notify_routes.log"


class EmailRoute(str, Enum):
    PICKS_UPDATE = "picks_update"
    ADMIN = "admin"


def log(message: str) -> None:
    """Append a timestamped line to logs/notify_routes.log.

    Wrapped defensively: a logging failure (e.g. a full disk) must never
    prevent the caller from getting its recipient list back. This module's
    entire purpose on the membership-filter path is "never fail closed", so
    that guarantee has to extend to its own logging too.
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    except OSError:
        pass


def _unique(recipients: Iterable[str]) -> List[str]:
    out: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        value = str(recipient or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        out.append(value)
        seen.add(key)
    return out


def _setting(env: Mapping[str, str], key: str) -> str:
    if key in env:
        return str(env.get(key) or "")
    return str(os.environ.get(key) or "")


def enforcement_enabled(env: Mapping[str, str]) -> bool:
    """MEMBERSHIP_ENFORCEMENT defaults to on; only an explicit "0" disables it.

    This is the kill switch: set MEMBERSHIP_ENFORCEMENT=0 to instantly stop
    all membership-based filtering without touching the membership store.
    """
    raw = _setting(env, "MEMBERSHIP_ENFORCEMENT").strip()
    return raw != "0"


def _effective_root(root: Path | None) -> Path:
    return root if root is not None else ROOT


def _resolve_relative_to_root(file_path: str, *, root: Path | None) -> str:
    """Resolve a possibly-relative recipient-file path against the effective
    root, instead of letting ``gmail_api_notify.parse_recipients`` resolve it
    against its own module-level ``ROOT`` (frozen at first import from the
    ambient ``QUANTCHECK_HOME``).

    This matters whenever a caller passes an explicit ``root`` that differs
    from the process's ambient QUANTCHECK_HOME -- e.g. ``quantcheck-admin
    --root <path>`` or a test root -- since otherwise a *relative*
    ``NOTIFY_EMAIL_FILE``/``NOTIFY_ADMIN_EMAIL_FILE`` would silently resolve
    against the wrong (real production) directory. An empty ``file_path``
    stays empty (it means "no file configured, inline recipients only") and
    an already-absolute path passes through unchanged, so this is a no-op
    for every existing caller that never passes ``root``.
    """
    if not file_path:
        return file_path
    path = Path(file_path)
    if path.is_absolute():
        return file_path
    return str(_effective_root(root) / path)


def _membership_store_path(env: Mapping[str, str], *, root: Path | None = None) -> Path:
    return membership_store.resolve_path(_effective_root(root), env)


def _classify_subscribers(
    recipients: List[str],
    env: Mapping[str, str],
    *,
    now: datetime | None = None,
    root: Path | None = None,
) -> tuple[List[str], List[dict], int, str | None]:
    """Classify subscriber emails by membership status.

    Returns ``(included, excluded, unknown_fail_open_count, store_error)``.
    Never raises. On any membership-store problem (missing file, corrupt
    JSON, malformed entry), fails open: ``included`` is the full input list,
    ``excluded`` is empty, and ``store_error`` carries a description for the
    caller to log loudly. This function itself does not log; callers decide
    what and how often to log (see ``subscriber_recipients`` for the actual
    send path, and ``route_preview`` for the silent read-only variant).
    """
    if now is None:
        now = datetime.now(membership.MEMBERSHIP_TZ)
    try:
        store = membership_store.load_store(_membership_store_path(env, root=root))
    except membership_store.MembershipStoreError as exc:
        return list(recipients), [], 0, str(exc)

    included: List[str] = []
    excluded: List[dict] = []
    unknown = 0
    for email in recipients:
        member = store.find(email)
        if member is None:
            # In the list, but the membership store has no record of them at
            # all: fail open (treat as active). This is intentionally
            # aggregated by the caller into a single count-only warning
            # rather than logged per-email, both to stay readable when many
            # subscribers predate the membership system and to avoid ever
            # writing subscriber addresses into logs on a path that is
            # exercised by unrelated tests.
            unknown += 1
            included.append(email)
            continue
        if member.is_effectively_active(now):
            included.append(email)
        else:
            excluded.append(
                {
                    "email": email,
                    "reason": member.effective_status(now),
                    "expires_at": member.expires_at.isoformat() if member.expires_at else None,
                }
            )
    return included, excluded, unknown, None


def subscriber_recipients(env: Mapping[str, str], *, now: datetime | None = None, root: Path | None = None) -> List[str]:
    """Subscriber recipients for the PICKS_UPDATE route, membership-filtered.

    Safety contract (see docs/MEMBERSHIP.md): this function must NEVER drop
    a recipient because of a membership-store problem. Every failure mode --
    missing file, corrupt JSON, a malformed row, an email with no matching
    member record -- fails open. The only thing that can shrink the returned
    list below the raw notify_recipients.txt contents is a member whose
    store record says, unambiguously, that they are expired or cancelled.

    ``root`` is optional and only needed by callers that operate on a
    non-default root (e.g. ``quantcheck-admin --root``); every existing
    caller that omits it keeps today's behavior exactly (resolve relative to
    this module's own ``ROOT``).
    """
    file_path = _resolve_relative_to_root(_setting(env, "NOTIFY_EMAIL_FILE"), root=root)
    recipients = parse_recipients(_setting(env, "NOTIFY_EMAIL_TO"), file_path=file_path)

    if not enforcement_enabled(env):
        log(f"membership filter: subscribers={len(recipients)} active={len(recipients)} excluded=0 enforcement=off")
        return recipients

    included, excluded, unknown, store_error = _classify_subscribers(recipients, env, now=now, root=root)

    if store_error is not None:
        log(
            "MEMBERSHIP STORE UNAVAILABLE - failing open, sending to all "
            f"{len(recipients)} subscriber(s) unfiltered: {store_error}"
        )
        return recipients

    log(f"membership filter: subscribers={len(recipients)} active={len(included)} excluded={len(excluded)} enforcement=on")
    if unknown:
        log(f"membership filter warning: {unknown} subscriber(s) not found in membership store; fail-open (treated as active)")
    if excluded:
        details = ", ".join(f"{item['email']}({item['reason']})" for item in excluded)
        log(f"membership filter excluded {len(excluded)} subscriber(s): {details}")
    return included


def admin_recipients(env: Mapping[str, str], *, root: Path | None = None) -> List[str]:
    """Admin recipients. Never membership-filtered, by design: admins are
    operators, not paying subscribers, and admin mail includes tracebacks
    and diagnostics that must keep flowing regardless of membership state.
    """
    file_path = _resolve_relative_to_root(_setting(env, "NOTIFY_ADMIN_EMAIL_FILE"), root=root)
    return parse_recipients(_setting(env, "NOTIFY_ADMIN_EMAIL_TO"), file_path=file_path)


def recipients_for_route(route: EmailRoute | str, env: Mapping[str, str], *, root: Path | None = None) -> List[str]:
    route = EmailRoute(route)
    subscribers = subscriber_recipients(env, root=root)
    admins = admin_recipients(env, root=root)
    if route == EmailRoute.PICKS_UPDATE:
        return _unique([*subscribers, *admins])
    return admins


def route_label(route: EmailRoute | str) -> str:
    route = EmailRoute(route)
    if route == EmailRoute.PICKS_UPDATE:
        return "picks update recipients"
    return "admin recipients"


def route_preview(route: EmailRoute | str, env: Mapping[str, str], *, now: datetime | None = None, root: Path | None = None) -> dict:
    """A read-only, JSON-serializable preview of who a route would reach.

    Unlike ``subscriber_recipients``, this never writes to the log -- it is
    meant to be called freely (by ``quantcheck-admin route preview``, the
    MCP server, and ``daily_admin_status``) without generating log noise on
    every inspection.
    """
    route = EmailRoute(route)
    enforcement = enforcement_enabled(env)
    admins = admin_recipients(env, root=root)

    raw_subscribers: List[str] = []
    if route != EmailRoute.ADMIN:
        file_path = _resolve_relative_to_root(_setting(env, "NOTIFY_EMAIL_FILE"), root=root)
        raw_subscribers = parse_recipients(_setting(env, "NOTIFY_EMAIL_TO"), file_path=file_path)

    excluded: List[dict] = []
    unknown = 0
    store_error: str | None = None
    if route == EmailRoute.ADMIN or not enforcement:
        included_subscribers = list(raw_subscribers)
    else:
        included_subscribers, excluded, unknown, store_error = _classify_subscribers(raw_subscribers, env, now=now, root=root)
        if store_error is not None:
            included_subscribers = list(raw_subscribers)
            excluded = []

    included = _unique([*included_subscribers, *admins])
    result: dict = {
        "route": route.value,
        "enforcement": enforcement,
        "included": included,
        "excluded": excluded,
        "counts": {
            "total": len(raw_subscribers) + len(admins),
            "included": len(included),
            "excluded": len(excluded),
            "unknown_fail_open": unknown,
            "subscribers_total": len(raw_subscribers),
            "admins_total": len(admins),
        },
    }
    if store_error is not None:
        result["store_error"] = store_error
    return result
