"""Storage layer for `state/memberships.json`.

This module owns the on-disk representation only: loading, saving, and the
per-member "what status is this, right now" derivation. Business operations
(add/extend/remove/migrate) live in `quantcheck.service.members`, which is
also responsible for keeping `notify_recipients.txt` in sync via
`quantcheck.recipients`.

`state/memberships.json` contains subscriber PII (email addresses) and must
never be committed; it is listed in `.gitignore` alongside its `.bak` files.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from quantcheck.membership import is_active
from quantcheck.recipients import normalize_email
from quantcheck.state import atomic_write_json

STORE_VERSION = 1
DEFAULT_TIMEZONE = "America/New_York"
STORE_FILE_ENV_KEY = "MEMBERSHIP_STORE_FILE"


class MembershipStoreError(RuntimeError):
    """The membership store file is missing, unreadable, or malformed.

    This is a distinct exception type on purpose: read paths that must FAIL
    OPEN on storage trouble (see `notify_routes.subscriber_recipients`) catch
    exactly this type and fall back to "treat every subscriber as valid"
    rather than risk a bare `except Exception` silently swallowing an
    unrelated bug. Never catch this broadly and discard membership data as a
    side effect -- see `load_or_create` for the one legitimate case (file
    does not exist yet) where a fresh empty store is appropriate instead of
    an error.
    """


def default_path(root: Path) -> Path:
    return Path(root) / "state" / "memberships.json"


def resolve_path(root: Path, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the effective memberships.json path for `root`/`env`.

    Honors a `MEMBERSHIP_STORE_FILE` override (absolute, or relative to
    `root`) exactly like `quantcheck.recipients.resolve_recipient_path`
    honors `NOTIFY_EMAIL_FILE`: if `env` is a partial mapping that doesn't
    contain the key at all, this falls back to `os.environ`, so both a
    fully-explicit test env dict and a real process environment behave the
    same way. This is the single source of truth for the store path --
    `notify_routes.py` and `quantcheck.service.members` both call this
    rather than each resolving it themselves.
    """
    env = env or {}
    if STORE_FILE_ENV_KEY in env:
        configured = str(env.get(STORE_FILE_ENV_KEY) or "").strip()
    else:
        configured = str(os.environ.get(STORE_FILE_ENV_KEY) or "").strip()
    if not configured:
        return default_path(root)
    path = Path(configured)
    return path if path.is_absolute() else (Path(root) / path)


def _parse_dt(value: Any, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MembershipStoreError(f"invalid {field_name}: {value!r}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise MembershipStoreError(f"{field_name} must be timezone-aware: {value!r}")
    return dt


def _dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


@dataclass
class Member:
    email: str
    status: str
    joined_at: datetime
    expires_at: datetime | None
    months_total: int = 0
    note: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalize on every construction path (direct call or from_dict), so
        # MembershipStore.upsert/find/remove can safely key on `.email`
        # without re-normalizing, and so migration/add/extend callers can
        # never accidentally create a case-variant duplicate of an existing
        # member.
        self.email = normalize_email(self.email)
        self.status = str(self.status or "active").strip().lower()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Member":
        email = normalize_email(str(data.get("email") or ""))
        if not email:
            raise MembershipStoreError(f"member entry missing a valid email: {data!r}")
        joined_at = _parse_dt(data.get("joined_at"), field_name=f"{email}.joined_at")
        if joined_at is None:
            raise MembershipStoreError(f"member {email} is missing joined_at")
        expires_at = _parse_dt(data.get("expires_at"), field_name=f"{email}.expires_at")
        try:
            months_total = int(data.get("months_total") or 0)
        except (TypeError, ValueError) as exc:
            raise MembershipStoreError(f"member {email} has invalid months_total: {data.get('months_total')!r}") from exc
        history = data.get("history") or []
        if not isinstance(history, list):
            raise MembershipStoreError(f"member {email} has malformed history (must be a list)")
        return cls(
            email=email,
            status=str(data.get("status") or "active").strip().lower(),
            joined_at=joined_at,
            expires_at=expires_at,
            months_total=months_total,
            note=str(data.get("note") or ""),
            history=list(history),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "status": self.status,
            "joined_at": _dt_to_str(self.joined_at),
            "expires_at": _dt_to_str(self.expires_at),
            "months_total": self.months_total,
            "note": self.note,
            "history": self.history,
        }

    def effective_status(self, now: datetime) -> str:
        """The status that actually governs behavior right now.

        `cancelled` is the one sticky stored value; everything else is
        re-derived from `expires_at` so a stale on-disk `status` (e.g. still
        says "active" long after `expires_at` passed) can never cause a
        lapsed member to keep receiving mail, and conversely can never cause
        a legacy/never-expiring member to be misread as expired.
        """
        if self.status == "cancelled":
            return "cancelled"
        if self.expires_at is None:
            return "legacy"
        return "active" if is_active(self.expires_at, now) else "expired"

    def is_effectively_active(self, now: datetime) -> bool:
        return self.effective_status(now) in ("active", "legacy")

    def add_history(
        self,
        *,
        action: str,
        months: int | None,
        expires_at: datetime | None,
        actor: str,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        self.history.append(
            {
                "at": _dt_to_str(at or datetime.now(timezone.utc)),
                "action": action,
                "months": months,
                "expires_at": _dt_to_str(expires_at),
                "actor": actor,
                "reason": reason,
            }
        )


@dataclass
class MembershipStore:
    path: Path
    version: int = STORE_VERSION
    timezone: str = DEFAULT_TIMEZONE
    members: list[Member] = field(default_factory=list)

    def find(self, email: str) -> Member | None:
        key = normalize_email(email)
        for member in self.members:
            if member.email == key:
                return member
        return None

    def upsert(self, member: Member) -> None:
        for index, existing in enumerate(self.members):
            if existing.email == member.email:
                self.members[index] = member
                return
        self.members.append(member)

    def remove(self, email: str) -> bool:
        key = normalize_email(email)
        before = len(self.members)
        self.members = [member for member in self.members if member.email != key]
        return len(self.members) != before

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "timezone": self.timezone,
            "members": [member.to_dict() for member in self.members],
        }


def load_store(path: Path) -> MembershipStore:
    """Strictly load the store from `path`.

    Raises `MembershipStoreError` if the file is missing, unreadable, not
    valid JSON, or structurally malformed. Use this directly on any path
    that must fail OPEN on error -- catch `MembershipStoreError` there, not
    a bare `except Exception`, so unrelated bugs are not accidentally
    absorbed by the same safety net.
    """
    path = Path(path)
    if not path.exists():
        raise MembershipStoreError(f"membership store not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MembershipStoreError(f"membership store unreadable: {path}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MembershipStoreError(f"membership store is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MembershipStoreError(f"membership store malformed (expected a JSON object): {path}")
    raw_members = raw.get("members", [])
    if not isinstance(raw_members, list):
        raise MembershipStoreError(f"membership store malformed (members must be a list): {path}")
    members = []
    for index, entry in enumerate(raw_members):
        try:
            members.append(Member.from_dict(entry))
        except MembershipStoreError:
            raise
        except Exception as exc:
            # Member.from_dict assumes a mapping; a non-dict entry (a bare
            # string, number, null, or list -- possible after a bad manual
            # edit) raises a raw AttributeError/TypeError instead of
            # MembershipStoreError. Every caller on the fail-open path (see
            # notify_routes._classify_subscribers) catches only
            # MembershipStoreError, so leaving this unwrapped would let one
            # malformed row crash the real send pipeline instead of failing
            # open like every other malformed-store case.
            raise MembershipStoreError(f"membership store malformed (entry {index} is not a valid member record): {entry!r}") from exc
    return MembershipStore(
        path=path,
        version=int(raw.get("version") or STORE_VERSION),
        timezone=str(raw.get("timezone") or DEFAULT_TIMEZONE),
        members=members,
    )


def load_or_create(path: Path) -> MembershipStore:
    """Load the store, or return a fresh empty one if the file does not exist.

    Only a genuinely *missing* file is treated as benign (first-ever write,
    e.g. the initial migration). A file that exists but fails to parse still
    raises `MembershipStoreError` -- corruption must never be silently
    discarded/overwritten.
    """
    path = Path(path)
    if not path.exists():
        return MembershipStore(path=path, version=STORE_VERSION, timezone=DEFAULT_TIMEZONE, members=[])
    return load_store(path)


def save_store(store: MembershipStore, *, backup: bool = True) -> Path | None:
    """Persist `store` atomically (tmp file + os.replace, via
    `quantcheck.state.atomic_write_json`), writing a timestamped `.bak` of
    the previous contents first when one exists. Mirrors the backup
    convention in `quantcheck.recipients.write_recipient_file`.
    """
    path = Path(store.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.{stamp}.bak")
        shutil.copy2(path, backup_path)
    atomic_write_json(path, store.to_dict())
    return backup_path
