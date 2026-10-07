"""Membership CRUD, backed by quantcheck.membership_store and kept in sync
with notify_recipients.txt via quantcheck.recipients (never reimplemented).

See quantcheck/service/__init__.py for the module contract: JSON-serializable
dict returns, no printing, ServiceError on expected failure.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from quantcheck import membership
from quantcheck import membership_store
from quantcheck import recipients
from quantcheck.config import get_root, load_env
from quantcheck.membership_store import Member, MembershipStore
from quantcheck.service.errors import ServiceError

VALID_STATUS_FILTERS = ("active", "expired", "cancelled", "legacy")
PAYMENT_KEYS = ("amount", "currency", "channel", "paid_at", "ref")
UNSPECIFIED = "unspecified"


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


# ---------------------------------------------------------------------------
# Argument helpers: expiry mode, payments, pasted email lists
# ---------------------------------------------------------------------------


_EMAIL_SPLIT_RE = re.compile(r"[\s,;]+")


def parse_email_list(source: str | Iterable[str] | None) -> list[str]:
    """Split a messy pasted blob into raw tokens.

    Separators are any whitespace, commas, semicolons and newlines; everything
    from a `#` to the end of its line is a comment and ignored. Tokens are
    returned as written (order preserved, no lowercasing, no validation, no
    de-duplication) so callers can report invalid ones back verbatim.
    """
    if source is None:
        return []
    chunks = [source] if isinstance(source, str) else [str(item) for item in source]
    tokens: list[str] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            body = line.split("#", 1)[0]
            tokens.extend(token for token in _EMAIL_SPLIT_RE.split(body) if token)
    return tokens


def _plan_expiry_mode(months: int | None, expires_at: str | None, align: bool) -> str:
    chosen = [name for name, given in (("months", months is not None), ("expires_at", expires_at is not None), ("align", bool(align))) if given]
    if len(chosen) != 1:
        raise ServiceError(
            "invalid_arguments",
            "exactly one of months, expires_at, align=True must be given" + (f" (got: {', '.join(chosen)})" if chosen else " (got none)"),
            {"given": chosen},
        )
    return chosen[0]


def _check_months(months: Any) -> int:
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        raise ServiceError("invalid_months", f"months must be a positive integer, got {months!r}", {"months": months})
    return months


def _parse_concrete_expiry(expires_at: str | None, *, field_name: str = "expires_at") -> datetime:
    parsed = _parse_optional_dt(expires_at, field_name=field_name)
    if parsed is None:
        raise ServiceError("invalid_expiry", f"{field_name} must be a concrete date or datetime, not null", {field_name: expires_at})
    return parsed


def _most_common_active_expiry(store: MembershipStore, now: datetime) -> datetime:
    """The most common `expires_at` among currently-active members (ties go to the later date)."""
    counts: Counter[datetime] = Counter(
        m.expires_at for m in store.members if m.expires_at is not None and m.effective_status(now) == "active"
    )
    if not counts:
        raise ServiceError("no_active_members", "align requires at least one currently-active member with an expiry to copy")
    best = max(counts.values())
    return max(expiry for expiry, count in counts.items() if count == best)


def _resolve_new_member_expiry(
    mode: str, months: int | None, expires_at: str | None, joined_dt: datetime, store: MembershipStore, now: datetime
) -> datetime:
    if mode == "months":
        return membership.expiry_for_new_member(joined_dt, _check_months(months))
    if mode == "expires_at":
        return _parse_concrete_expiry(expires_at)
    return _most_common_active_expiry(store, now)


def _normalize_payment(payment: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """Validate a payment dict and return the canonical stored form
    (`amount`, `currency`, `channel`, `paid_at`, `ref`; absent optionals are None).
    """
    if payment is None:
        return None
    if not isinstance(payment, Mapping):
        raise ServiceError("invalid_payment", "payment must be an object/dict", {"payment": str(payment)})
    unknown = sorted(set(payment) - set(PAYMENT_KEYS))
    if unknown:
        raise ServiceError("invalid_payment", f"unknown payment fields: {unknown}", {"unknown": unknown})
    raw_amount = payment.get("amount")
    if raw_amount is None or isinstance(raw_amount, bool):
        raise ServiceError("invalid_payment", "payment.amount is required and must be a number", {"amount": raw_amount})
    try:
        amount = Decimal(str(raw_amount).strip())
    except InvalidOperation as exc:
        raise ServiceError("invalid_payment", f"payment.amount must be a number, got {raw_amount!r}", {"amount": raw_amount}) from exc
    if not amount.is_finite() or amount <= 0:
        raise ServiceError("invalid_payment", f"payment.amount must be a positive finite number, got {raw_amount!r}", {"amount": raw_amount})
    stored_amount: int | float = int(amount) if amount == amount.to_integral_value() else float(amount)

    def _optional_text(key: str, transform) -> str | None:
        value = payment.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ServiceError("invalid_payment", f"payment.{key} must be a string", {key: value})
        text = value.strip()
        return transform(text) if text else None

    paid_at_raw = payment.get("paid_at")
    paid_at = _parse_optional_dt(paid_at_raw, field_name="payment.paid_at") if paid_at_raw not in (None, "") else None
    return {
        "amount": stored_amount,
        "currency": _optional_text("currency", str.upper),
        "channel": _optional_text("channel", str.lower),
        "paid_at": (paid_at or now).isoformat(),
        "ref": _optional_text("ref", str),
    }


def build_payment(
    amount: Any = None, currency: str | None = None, channel: str | None = None, paid_at: str | None = None, ref: str | None = None
) -> dict[str, Any] | None:
    """Convenience for front ends: assemble a payment dict from flat fields, or None if nothing was given."""
    fields = {"amount": amount, "currency": currency, "channel": channel, "paid_at": paid_at, "ref": ref}
    given = {key: value for key, value in fields.items() if value is not None}
    return given or None


def _payment_entries(member: Member) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in member.history:
        if not isinstance(item, dict) or not isinstance(item.get("payment"), dict):
            continue
        payment = item["payment"]
        entries.append(
            {
                "email": member.email,
                "paid_at": payment.get("paid_at") or item.get("at"),
                "amount": payment.get("amount"),
                "currency": payment.get("currency"),
                "channel": payment.get("channel"),
                "ref": payment.get("ref"),
                "action": item.get("action"),
                "at": item.get("at"),
            }
        )
    return entries


def _sum_amounts(rows: Iterable[tuple[str, Any]]) -> dict[str, int | float]:
    totals: dict[str, Decimal] = {}
    for key, amount in rows:
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            continue
        if not value.is_finite():
            continue
        totals[key] = totals.get(key, Decimal(0)) + value
    return {key: (int(v) if v == v.to_integral_value() else float(v)) for key, v in sorted(totals.items())}


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


def _add_many_to_recipients_file(root: Path, env: Mapping[str, str], emails: Sequence[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Batch version of `_add_to_recipients_file`: at most one file write for all of `emails`."""
    recipient_file = recipients.load_recipient_file(env, "subscriber", root)
    existing = set(recipient_file.entries)
    to_add = [email for email in recipients.unique_emails(emails) if email not in existing]
    result: dict[str, Any] = {"changed": False, "path": str(recipient_file.path), "added": to_add}
    if not to_add or dry_run:
        return result
    entries = recipients.unique_emails([*recipient_file.entries, *to_add])
    backup_path = recipients.write_recipient_file(recipient_file, entries)
    result.update({"changed": True, "backup": str(backup_path) if backup_path else None})
    return result


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
    detail = _member_detail(member, now)
    payments = _payment_entries(member)
    detail["payments"] = payments
    detail["total_paid"] = _sum_amounts((p["currency"] or UNSPECIFIED, p["amount"]) for p in payments)
    return {"member": detail}


def add_member(
    email: str,
    months: int | None = None,
    note: str | None = None,
    joined_at: str | None = None,
    *,
    expires_at: str | None = None,
    align: bool = False,
    payment: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Create a new member. Exactly one of `months` (next-anchor arithmetic),
    `expires_at` (explicit date) or `align=True` (copy the most common expiry
    among active members) chooses the expiry.
    """
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    mode = _plan_expiry_mode(months, expires_at, align)
    now = _now()
    joined_dt = _parse_optional_dt(joined_at, field_name="joined_at") or now
    normalized_payment = _normalize_payment(payment, now)

    store = _load_store(resolved_root, env)
    if store.find(normalized) is not None:
        raise ServiceError("member_already_exists", f"{normalized} is already a member; use extend_member to add months", {"email": normalized})

    new_expiry = _resolve_new_member_expiry(mode, months, expires_at, joined_dt, store, now)
    months_total = months if mode == "months" else 0
    member = Member(email=normalized, status="active", joined_at=joined_dt, expires_at=new_expiry, months_total=months_total, note=note or "")
    member.add_history(
        action="add",
        months=months if mode == "months" else None,
        expires_at=new_expiry,
        actor="service.add_member",
        at=now,
        extra=_history_extra(mode, normalized_payment),
    )
    store.upsert(member)
    store_backup = _save_store(store)

    recipients_result = _add_to_recipients_file(resolved_root, env, normalized)
    return {"member": _member_detail(member, now), "recipients_file": recipients_result, "store_backup": store_backup}


def _history_extra(mode: str | None, payment: dict[str, Any] | None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if mode is not None:
        extra["mode"] = mode
    if payment is not None:
        extra["payment"] = payment
    return extra


def _apply_extend(member: Member, months: int, note: str | None, now: datetime, payment: dict[str, Any] | None, actor: str) -> datetime:
    """Shared by extend_member and bulk_extend: mutates `member` in memory only."""
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
    member.add_history(action="extend", months=months, expires_at=new_expiry, actor=actor, at=now, extra=_history_extra(None, payment))
    return new_expiry


def extend_member(
    email: str,
    months: int,
    note: str | None = None,
    *,
    payment: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()
    months = _check_months(months)
    normalized_payment = _normalize_payment(payment, now)

    store = _load_store(resolved_root, env)
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}; use add_member to create one", {"email": normalized})

    _apply_extend(member, months, note, now, normalized_payment, "service.extend_member")
    store.upsert(member)
    store_backup = _save_store(store)

    # Re-adds them to notify_recipients.txt if a prior remove_member had
    # taken them out; a no-op otherwise.
    recipients_result = _add_to_recipients_file(resolved_root, env, normalized)
    return {"member": _member_detail(member, now), "recipients_file": recipients_result, "store_backup": store_backup}


def _apply_set_expiry(member: Member, new_expiry: datetime | None, note: str, now: datetime, actor: str) -> None:
    """Shared by set_expiry and bulk_set_expiry: mutates `member` in memory only."""
    member.expires_at = new_expiry
    member.add_history(action="set_expiry", months=None, expires_at=new_expiry, actor=actor, at=now, reason=note)


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
    _apply_set_expiry(member, new_expiry, note, now, "service.set_expiry")
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


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def record_payment(
    email: str,
    amount: Any,
    currency: str | None = None,
    channel: str | None = None,
    paid_at: str | None = None,
    ref: str | None = None,
    note: str | None = None,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Append a `payment` history entry. Never changes expiry, status,
    months_total, the note field, or notify_recipients.txt -- it only records
    money received (use extend_member / add_member to also grant time).
    """
    resolved_root, env = _context(root)
    normalized = _validate_email(email)
    now = _now()
    normalized_payment = _normalize_payment({"amount": amount, "currency": currency, "channel": channel, "paid_at": paid_at, "ref": ref}, now)

    store = _load_store(resolved_root, env)
    member = store.find(normalized)
    if member is None:
        raise ServiceError("member_not_found", f"no member found for {normalized}", {"email": normalized})

    member.add_history(
        action="payment",
        months=None,
        expires_at=member.expires_at,
        actor="service.record_payment",
        at=now,
        reason=note,
        extra={"payment": normalized_payment},
    )
    store.upsert(member)
    store_backup = _save_store(store)
    detail = get_member(normalized, root=resolved_root)["member"]
    return {"member": detail, "payment": normalized_payment, "store_backup": store_backup}


def payments_report(since: str | None = None, until: str | None = None, *, root: Path | str | None = None) -> dict[str, Any]:
    """All recorded payments, optionally filtered on `paid_at` (`since`
    inclusive; a bare-date `until` includes that whole day), with totals by
    currency and by channel (channel -> currency -> sum). Read-only.
    """
    resolved_root, env = _context(root)
    since_dt = _parse_optional_dt(since, field_name="since")
    until_dt = _parse_optional_dt(until, field_name="until")
    until_exclusive = False
    if until_dt is not None and len(str(until).strip()) <= 10:
        until_dt = until_dt + timedelta(days=1)
        until_exclusive = True

    store = _load_store(resolved_root, env)
    rows: list[tuple[datetime | None, dict[str, Any]]] = []
    for member in store.members:
        for entry in _payment_entries(member):
            try:
                paid = _parse_optional_dt(entry["paid_at"], field_name="paid_at") if entry["paid_at"] else None
            except ServiceError:
                paid = None
            if since_dt is not None and (paid is None or paid < since_dt):
                continue
            if until_dt is not None and (paid is None or paid > until_dt or (until_exclusive and paid == until_dt)):
                continue
            rows.append((paid, entry))
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda row: (row[0] or floor, row[1]["email"]))
    payments = [{key: value for key, value in entry.items() if key != "at"} for _, entry in rows]

    by_channel: dict[str, dict[str, int | float]] = {}
    for channel in sorted({p["channel"] or UNSPECIFIED for p in payments}):
        by_channel[channel] = _sum_amounts((p["currency"] or UNSPECIFIED, p["amount"]) for p in payments if (p["channel"] or UNSPECIFIED) == channel)
    return {
        "since": since_dt.isoformat() if since_dt else None,
        "until": until,
        "count": len(payments),
        "totals_by_currency": _sum_amounts((p["currency"] or UNSPECIFIED, p["amount"]) for p in payments),
        "totals_by_channel": by_channel,
        "payments": payments,
    }


# ---------------------------------------------------------------------------
# Bulk operations (dry-run by default; load once, save once, one recipients write)
# ---------------------------------------------------------------------------


def _coerce_emails(emails: str | Iterable[str] | None) -> list[str]:
    return parse_email_list(emails)


def _bulk_result(email: str, outcome: str, code: str | None = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"email": email, "outcome": outcome, "code": code, "message": message, **extra}


def _bulk_summary(
    dry_run: bool, requested: int, duplicates: int, results: list[dict[str, Any]], recipients_result: dict[str, Any] | None, store_backup: str | None, **extra: Any
) -> dict[str, Any]:
    skipped = [r for r in results if r["outcome"] == "skipped"]
    by_code = dict(sorted(Counter(r["code"] for r in skipped).items()))
    return {
        "dry_run": dry_run,
        **extra,
        "counts": {
            "requested": requested,
            "duplicates_ignored": duplicates,
            "ok": len(results) - len(skipped),
            "skipped": len(skipped),
            "skipped_by_code": by_code,
        },
        "results": results,
        "recipients_file": recipients_result,
        "store_backup": store_backup,
    }


def _dedupe_tokens(tokens: Sequence[str]) -> tuple[list[tuple[str, str | None]], int]:
    """-> ([(token_or_normalized_email, normalized_or_None_if_invalid)], duplicates_ignored)"""
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    duplicates = 0
    for token in tokens:
        normalized = recipients.normalize_email(token)
        if not normalized or not recipients.is_valid_email(normalized):
            key = f"invalid:{token}"
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            out.append((token, None))
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        out.append((normalized, normalized))
    return out, duplicates


def bulk_add(
    emails: str | Iterable[str] | None,
    months: int | None = None,
    expires_at: str | None = None,
    align: bool = False,
    note: str | None = None,
    payment: Mapping[str, Any] | None = None,
    dry_run: bool = True,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Add many new members with the same expiry rule (one of months /
    expires_at / align). Invalid addresses and existing members are reported
    and skipped. `payment` (if given) is recorded on EVERY added member --
    amount is per member, not a total. `align` is resolved once, against the
    store as it was before this call.
    """
    resolved_root, env = _context(root)
    tokens = _coerce_emails(emails)
    if not tokens:
        raise ServiceError("invalid_arguments", "emails is empty")
    mode = _plan_expiry_mode(months, expires_at, align)
    now = _now()
    if mode == "months":
        _check_months(months)
    normalized_payment = _normalize_payment(payment, now)

    store = _load_store(resolved_root, env)
    shared_expiry = _resolve_new_member_expiry(mode, months, expires_at, now, store, now)
    deduped, duplicates = _dedupe_tokens(tokens)

    results: list[dict[str, Any]] = []
    added: list[str] = []
    for token, email in deduped:
        if email is None:
            results.append(_bulk_result(token, "skipped", "invalid_email", f"invalid email address: {token!r}"))
            continue
        if store.find(email) is not None:
            results.append(_bulk_result(email, "skipped", "already_member", f"{email} is already a member; use bulk_extend"))
            continue
        new_expiry = shared_expiry
        member = Member(email=email, status="active", joined_at=now, expires_at=new_expiry, months_total=months if mode == "months" else 0, note=note or "")
        member.add_history(
            action="add",
            months=months if mode == "months" else None,
            expires_at=new_expiry,
            actor="service.bulk_add",
            at=now,
            extra=_history_extra(mode, normalized_payment),
        )
        store.upsert(member)
        added.append(email)
        results.append(_bulk_result(email, "ok", action="added", expires_at=new_expiry.isoformat(), months=months if mode == "months" else None))

    store_backup: str | None = None
    if added and not dry_run:
        store_backup = _save_store(store)
    recipients_result = _add_many_to_recipients_file(resolved_root, env, added, dry_run=dry_run) if added else None
    return _bulk_summary(dry_run, len(tokens), duplicates, results, recipients_result, store_backup, mode=mode, payment=normalized_payment)


def _select_bulk_targets(
    store: MembershipStore, emails: str | Iterable[str] | None, all_active: bool, now: datetime
) -> tuple[list[tuple[str, Member | None, dict[str, Any] | None]], int, int]:
    """-> (plan in input order as (email, member, skip_result), requested, duplicates).
    Exactly one of member / skip_result is set per entry.
    """
    tokens = _coerce_emails(emails)
    if all_active:
        if tokens:
            raise ServiceError("invalid_arguments", "give either emails or all_active=True, not both")
        plan = [(m.email, m, None) for m in sorted(store.members, key=lambda m: m.email) if m.effective_status(now) == "active"]
        return plan, len(plan), 0
    if not tokens:
        raise ServiceError("invalid_arguments", "give emails or all_active=True")
    deduped, duplicates = _dedupe_tokens(tokens)
    plan = []
    for token, email in deduped:
        if email is None:
            plan.append((token, None, _bulk_result(token, "skipped", "invalid_email", f"invalid email address: {token!r}")))
            continue
        member = store.find(email)
        if member is None:
            plan.append((email, None, _bulk_result(email, "skipped", "member_not_found", f"no member found for {email}")))
        else:
            plan.append((email, member, None))
    return plan, len(tokens), duplicates


def bulk_extend(
    emails: str | Iterable[str] | None = None,
    months: int | None = None,
    all_active: bool = False,
    note: str | None = None,
    dry_run: bool = True,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """`extend_member` for many members. Exactly one of `emails` /
    `all_active=True` (currently-active members only; legacy and expired are
    not touched). Cancelled members named explicitly are reinstated, same as
    extend_member.
    """
    resolved_root, env = _context(root)
    months = _check_months(months)
    now = _now()
    store = _load_store(resolved_root, env)
    plan, requested, duplicates = _select_bulk_targets(store, emails, all_active, now)

    results: list[dict[str, Any]] = []
    touched: list[str] = []
    for email, member, skipped in plan:
        if member is None:
            results.append(skipped)  # type: ignore[arg-type]
            continue
        previous = member.expires_at.isoformat() if member.expires_at else None
        was_status = member.effective_status(now)
        new_expiry = _apply_extend(member, months, note, now, None, "service.bulk_extend")
        store.upsert(member)
        touched.append(email)
        results.append(
            _bulk_result(email, "ok", action="extended", previous_expires_at=previous, expires_at=new_expiry.isoformat(), months=months, previous_status=was_status)
        )

    store_backup: str | None = None
    if touched and not dry_run:
        store_backup = _save_store(store)
    recipients_result = _add_many_to_recipients_file(resolved_root, env, touched, dry_run=dry_run) if touched else None
    return _bulk_summary(dry_run, requested, duplicates, results, recipients_result, store_backup, months=months)


def bulk_set_expiry(
    emails: str | Iterable[str] | None = None,
    expires_at: str | None = None,
    all_active: bool = False,
    note: str | None = None,
    dry_run: bool = True,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """`set_expiry` for many members (pure date correction; never touches
    status or notify_recipients.txt). `expires_at` must be concrete.
    """
    resolved_root, env = _context(root)
    new_expiry = _parse_concrete_expiry(expires_at)
    reason = note if note is not None else "bulk set_expiry via service"
    now = _now()
    store = _load_store(resolved_root, env)
    plan, requested, duplicates = _select_bulk_targets(store, emails, all_active, now)

    results: list[dict[str, Any]] = []
    changed = 0
    for email, member, skipped in plan:
        if member is None:
            results.append(skipped)  # type: ignore[arg-type]
            continue
        previous = member.expires_at.isoformat() if member.expires_at else None
        _apply_set_expiry(member, new_expiry, reason, now, "service.bulk_set_expiry")
        store.upsert(member)
        changed += 1
        results.append(_bulk_result(email, "ok", action="expiry_set", previous_expires_at=previous, expires_at=new_expiry.isoformat()))

    store_backup: str | None = None
    if changed and not dry_run:
        store_backup = _save_store(store)
    return _bulk_summary(dry_run, requested, duplicates, results, None, store_backup, expires_at=new_expiry.isoformat())
