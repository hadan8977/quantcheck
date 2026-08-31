"""Membership CRUD, backed by quantcheck.membership_store and kept in sync
with notify_recipients.txt via quantcheck.recipients (never reimplemented).

See quantcheck/service/__init__.py for the module contract: JSON-serializable
dict returns, no printing, ServiceError on expected failure.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from quantcheck import membership
from quantcheck import membership_store
from quantcheck import recipients
from quantcheck.config import get_root, load_env
from quantcheck.membership_store import Member, MembershipStore
from quantcheck.service.errors import ServiceError

VALID_STATUS_FILTERS = ("active", "expired", "cancelled", "legacy")


def _context(root: Path | str | None) -> tuple[Path, dict]:
    resolved_root = Path(root) if root is not None else get_root()
    env = load_env(resolved_root, override=True)
    return resolved_root, env


def _now() -> datetime:
    return datetime.now(membership.MEMBERSHIP_TZ)


def _validate_email(email: str) -> str:
    normalized = recipients.normalize_email(str(email or ""))
    if not normalized or not recipients.is_valid_email(normalized):
        raise ServiceError("invalid_email", f"invalid email address: {email!r}", {"email": email})
    return normalized


def _parse_optional_dt(value: str | None, *, field_name: str = "date") -> datetime | None:
    """Parse a bare date ("2026-10-09", assumed 00:00:00 NY) or a full ISO
    datetime. None, "", "null", and "none" all mean "no expiry" (legacy).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    if len(text) <= 10:
        parts = text.split("-")
        if len(parts) != 3:
            raise ServiceError("invalid_date", f"expected YYYY-MM-DD or full ISO datetime for {field_name}, got {value!r}")
        try:
            year, month, day = (int(part) for part in parts)
            return datetime(year, month, day, 0, 0, 0, tzinfo=membership.MEMBERSHIP_TZ)
        except ValueError as exc:
            raise ServiceError("invalid_date", f"expected YYYY-MM-DD or full ISO datetime for {field_name}, got {value!r}") from exc
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ServiceError("invalid_date", f"expected YYYY-MM-DD or full ISO datetime for {field_name}, got {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=membership.MEMBERSHIP_TZ)
    return dt.astimezone(membership.MEMBERSHIP_TZ)


def _load_store(root: Path, env: Mapping[str, str]) -> MembershipStore:
    path = membership_store.resolve_path(root, env)
    try:
        return membership_store.load_or_create(path)
    except membership_store.MembershipStoreError as exc:
        raise ServiceError("membership_store_error", str(exc), {"path": str(path)}) from exc


def _save_store(store: MembershipStore) -> str | None:
    backup_path = membership_store.save_store(store)
    return str(backup_path) if backup_path else None


def _member_summary(member: Member, now: datetime) -> dict[str, Any]:
    return {
        "email": member.email,
        "status": member.effective_status(now),
        "stored_status": member.status,
        "joined_at": member.joined_at.isoformat(),
        "expires_at": member.expires_at.isoformat() if member.expires_at else None,
        "months_total": member.months_total,
        "note": member.note,
    }


def _member_detail(member: Member, now: datetime) -> dict[str, Any]:
    data = _member_summary(member, now)
    data["history"] = member.history
    return data


def _add_to_recipients_file(root: Path, env: Mapping[str, str], email: str) -> dict[str, Any]:
    recipient_file = recipients.load_recipient_file(env, "subscriber", root)
    if email in set(recipient_file.entries):
        return {"changed": False, "path": str(recipient_file.path)}
    entries = recipients.unique_emails([*recipient_file.entries, email])
    backup_path = recipients.write_recipient_file(recipient_file, entries)
    return {"changed": True, "path": str(recipient_file.path), "backup": str(backup_path) if backup_path else None}


def _remove_from_recipients_file(root: Path, env: Mapping[str, str], email: str) -> dict[str, Any]:
    recipient_file = recipients.load_recipient_file(env, "subscriber", root)
    if email not in set(recipient_file.entries):
        return {"changed": False, "path": str(recipient_file.path)}
    entries = [existing for existing in recipient_file.entries if existing != email]
    backup_path = recipients.write_recipient_file(recipient_file, entries)
    return {"changed": True, "path": str(recipient_file.path), "backup": str(backup_path) if backup_path else None}


def list_members(status: str | None = None, expiring_within_days: int | None = None, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    store = _load_store(resolved_root, env)
    now = _now()

    if status is not None and status not in VALID_STATUS_FILTERS:
        raise ServiceError("invalid_status", f"status must be one of {VALID_STATUS_FILTERS}, got {status!r}", {"status": status})

    members = list(store.members)
    if status is not None:
        members = [m for m in members if m.effective_status(now) == status]
    if expiring_within_days is not None:
        cutoff = now + timedelta(days=expiring_within_days)
        members = [
            m for m in members
            if m.effective_status(now) == "active" and m.expires_at is not None and now <= m.expires_at <= cutoff
        ]
    members.sort(key=lambda m: m.email)
    return {"count": len(members), "members": [_member_summary(m, now) for m in members]}


def get_member(email: str, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    store = _load_store(resolved_root, env)
    now = _now()
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}", {"email": normalized})
    return {"member": _member_detail(member, now)}


def add_member(email: str, months: int, note: str | None = None, joined_at: str | None = None, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()
    joined_dt = _parse_optional_dt(joined_at, field_name="joined_at") or now

    store = _load_store(resolved_root, env)
    if store.find(normalized) is not None:
        raise ServiceError("member_already_exists", f"{normalized} is already a member; use extend_member to add months", {"email": normalized})

    expires_at = membership.expiry_for_new_member(joined_dt, months)
    member = Member(email=normalized, status="active", joined_at=joined_dt, expires_at=expires_at, months_total=months, note=note or "")
    member.add_history(action="add", months=months, expires_at=expires_at, actor="service.add_member", at=now)
    store.upsert(member)
    store_backup = _save_store(store)

    recipients_result = _add_to_recipients_file(resolved_root, env, normalized)
    return {"member": _member_detail(member, now), "recipients_file": recipients_result, "store_backup": store_backup}


def extend_member(email: str, months: int, note: str | None = None, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()

    store = _load_store(resolved_root, env)
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}; use add_member to create one", {"email": normalized})

    new_expiry = membership.extend_expiry(member.expires_at, months, now)
    member.expires_at = new_expiry
    member.months_total = (member.months_total or 0) + months
    if member.status == "cancelled":
        # Paying again is an unambiguous signal to reinstate a cancelled
        # member; otherwise extend_member would silently do nothing useful
        # (the filter would keep excluding them despite the new expiry).
        member.status = "active"
    if note is not None:
        member.note = note
    member.add_history(action="extend", months=months, expires_at=new_expiry, actor="service.extend_member", at=now)
    store.upsert(member)
    store_backup = _save_store(store)

    # Re-adds them to notify_recipients.txt if a prior remove_member had
    # taken them out; a no-op otherwise.
    recipients_result = _add_to_recipients_file(resolved_root, env, normalized)
    return {"member": _member_detail(member, now), "recipients_file": recipients_result, "store_backup": store_backup}


def set_expiry(email: str, expires_at: str | None, note: str, *, root: Path | str | None = None) -> dict[str, Any]:
    """Manual correction tool: sets expires_at (or null for legacy/never-expires)
    and records why. Deliberately does not touch `status` -- a date fix
    should not have the side effect of reinstating a cancelled member; use
    extend_member for that.
    """
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()

    store = _load_store(resolved_root, env)
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}", {"email": normalized})

    new_expiry = _parse_optional_dt(expires_at, field_name="expires_at")
    member.expires_at = new_expiry
    member.add_history(action="set_expiry", months=None, expires_at=new_expiry, actor="service.set_expiry", at=now, reason=note)
    store.upsert(member)
    store_backup = _save_store(store)
    return {"member": _member_detail(member, now), "store_backup": store_backup}


def remove_member(email: str, reason: str, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()

    store = _load_store(resolved_root, env)
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}", {"email": normalized})

    member.status = "cancelled"
    member.add_history(action="remove", months=None, expires_at=member.expires_at, actor="service.remove_member", at=now, reason=reason)
    store.upsert(member)
    store_backup = _save_store(store)

    recipients_result = _remove_from_recipients_file(resolved_root, env, normalized)
    return {"member": _member_detail(member, now), "recipients_file": recipients_result, "store_backup": store_backup}


def expiring_report(within_days: int = 14, *, root: Path | str | None = None) -> dict[str, Any]:
    resolved_root, env = _context(root)
    store = _load_store(resolved_root, env)
    now = _now()
    cutoff = now + timedelta(days=within_days)

    active = [m for m in store.members if m.effective_status(now) == "active"]
    expiring = sorted(
        (m for m in active if m.expires_at is not None and m.expires_at <= cutoff),
        key=lambda m: m.expires_at,
    )
    expired = [m for m in store.members if m.effective_status(now) == "expired"]
    cancelled = [m for m in store.members if m.effective_status(now) == "cancelled"]
    legacy = [m for m in store.members if m.effective_status(now) == "legacy"]

    return {
        "as_of": now.isoformat(),
        "within_days": within_days,
        "active_count": len(active),
        "expiring_count": len(expiring),
        "expiring": [_member_summary(m, now) for m in expiring],
        "expired_count": len(expired),
        "cancelled_count": len(cancelled),
        "legacy_count": len(legacy),
        "total_count": len(store.members),
    }


def sync_recipients(*, root: Path | str | None = None) -> dict[str, Any]:
    """Reconcile memberships.json against notify_recipients.txt. Read-only:
    reports drift, never modifies either side.
    """
    resolved_root, env = _context(root)
    store = _load_store(resolved_root, env)
    now = _now()

    recipient_file = recipients.load_recipient_file(env, "subscriber", resolved_root)
    file_emails = set(recipient_file.entries) | set(recipient_file.inline)

    all_member_emails = {m.email for m in store.members}
    active_member_emails = {m.email for m in store.members if m.is_effectively_active(now)}

    unknown_to_store = sorted(file_emails - all_member_emails)
    known_but_inactive_in_file = sorted((file_emails & all_member_emails) - active_member_emails)
    active_missing_from_file = sorted(active_member_emails - file_emails)

    return {
        "as_of": now.isoformat(),
        "recipients_file": str(recipient_file.path),
        "file_count": len(file_emails),
        "member_count": len(all_member_emails),
        "active_member_count": len(active_member_emails),
        "drift": {
            "in_file_unknown_to_store": unknown_to_store,
            "in_file_but_known_inactive": known_but_inactive_in_file,
            "active_members_missing_from_file": active_missing_from_file,
        },
        "in_sync": not unknown_to_store and not known_but_inactive_in_file and not active_missing_from_file,
    }


def migrate_from_recipients(expires_at: str, note: str, dry_run: bool = True, *, root: Path | str | None = None) -> dict[str, Any]:
    """One-time bulk migration: every address currently in
    notify_recipients.txt that has no membership record yet gets one, with
    the given expires_at/note. Idempotent: emails already present in the
    store are left untouched (never re-added, never overwritten), so
    running this again after manual corrections is always safe.
    """
    resolved_root, env = _context(root)
    normalized_expiry = _parse_optional_dt(expires_at, field_name="expires_at")
    if normalized_expiry is None:
        raise ServiceError("invalid_expiry", "migrate requires a concrete expires_at (null/legacy is not allowed for a bulk migration)", {"expires_at": expires_at})

    recipient_file = recipients.load_recipient_file(env, "subscriber", resolved_root)
    source_emails = recipients.unique_emails([*recipient_file.entries, *recipient_file.inline])

    store = _load_store(resolved_root, env)
    now = _now()

    to_add = [email for email in source_emails if store.find(email) is None]
    already_present = [email for email in source_emails if store.find(email) is not None]

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "source_file": str(recipient_file.path),
        "source_count": len(source_emails),
        "already_migrated_count": len(already_present),
        "to_migrate_count": len(to_add),
        "to_migrate": to_add,
        "expires_at": normalized_expiry.isoformat(),
    }
    if dry_run:
        return result

    recipients_file_backup: str | None = None
    if recipient_file.path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = recipient_file.path.with_name(f"{recipient_file.path.name}.{stamp}.bak")
        shutil.copy2(recipient_file.path, backup_path)
        recipients_file_backup = str(backup_path)

    for email in to_add:
        member = Member(email=email, status="active", joined_at=now, expires_at=normalized_expiry, months_total=0, note=note)
        member.add_history(action="migrate", months=None, expires_at=normalized_expiry, actor="migration", at=now)
        store.upsert(member)

    store_backup = _save_store(store) if to_add else None

    result.update(
        {
            "migrated_count": len(to_add),
            "migrated": to_add,
            "recipients_file_backup": recipients_file_backup,
            "store_backup": store_backup,
        }
    )
    return result
