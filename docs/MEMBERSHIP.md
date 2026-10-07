# Membership

quantcheck gates the `PICKS_UPDATE` mail route (picks-update reports and
forwarded official Quant GT email) by a paid-membership expiry date. This
document covers the billing-window rule, the storage format, the 2026-08-31
migration of the pre-existing subscriber list, and the operational cliff
that migration creates on 2026-10-09.

## The 9th-of-the-month anchor rule

Billing windows are anchored to the 9th of every month at `00:00:00
America/New_York`:

```
[9th 00:00 NY, next 9th 00:00 NY)
```

Equivalently: a membership window runs from the 9th at midnight through the
8th at 24:00 the following month. All the logic lives in
`quantcheck/membership.py`, which is pure date arithmetic with no I/O --
read it before touching anything membership-related, it is the foundation
everything else (`membership_store.py`, `service/members.py`,
`notify_routes.py`) sits on.

Key properties, enforced by `tests/test_membership.py`:

- **No proration.** The first "billing month" for a new member always ends
  at the very next anchor, no matter how little of the current partial
  window is left. Joining one hour before an anchor and buying one month of
  membership yields an expiry one hour later. This is the literal, intended
  rule, not a bug.
- **Anchor day 9 exists in every month**, so month arithmetic never needs
  the day-31-style end-of-month clamping a different anchor day would need.
- **Renewals stack on the existing expiry**, not on today's date, as long as
  the member is still active. An expired (or brand-new) member's renewal
  restarts from now instead.
- **DST is a non-issue.** Every anchor is pinned to `00:00:00` local time,
  and America/New_York's DST transitions always happen between 01:00 and
  03:00 local time, so an anchor can never land inside the ambiguous
  ("fall back") or nonexistent ("spring forward") wall-clock hour. See
  `tests/test_membership.py::DstTests`.

### Dated examples (verified live via `quantcheck-admin`, not just unit tests)

| Scenario | Result |
|---|---|
| Join 2026-08-10, buy 1 month | Expires **2026-09-09T00:00:00-04:00** |
| Join 2026-08-10, buy 3 months | Expires **2026-11-09T00:00:00-05:00** (November is past the fall DST change, hence `-05:00`) |
| Join 2026-09-08 23:00, buy 1 month | Expires **2026-09-09T00:00:00-04:00** (one hour of membership -- correct, see "no proration" above) |
| Active member, current expiry 2026-09-09, renews 3 months | New expiry **2026-12-09T00:00:00** (from the *existing* expiry, not from today) |
| Expired member (expiry in the past) renews 1 month | Expiry recomputed fresh **from now**, exactly like a new member |
| 12-month renewal from an 2026-08-10 join | Expires **2027-08-09** (crosses the year boundary correctly) |

## Storage: `state/memberships.json`

Never committed (see `.gitignore`; also covered by the blanket `state/`
rule, but PII gets an explicit rule too). Contains subscriber email
addresses and billing dates.

```json
{
  "version": 1,
  "timezone": "America/New_York",
  "members": [
    {
      "email": "x@y.com",
      "status": "active",
      "joined_at": "2026-08-31T08:38:54.315975-04:00",
      "expires_at": "2026-10-09T00:00:00-04:00",
      "months_total": 0,
      "note": "migrated from notify_recipients.txt on 2026-08-31",
      "history": [
        {"at": "2026-08-31T08:38:54.315975-04:00", "action": "migrate",
         "months": null, "expires_at": "2026-10-09T00:00:00-04:00",
         "actor": "migration", "reason": null}
      ]
    }
  ]
}
```

`status` is written to disk but **is not the source of truth on read**:
`quantcheck/membership_store.py:Member.effective_status()` recomputes it
from `expires_at` and the current time every time, so a stale on-disk
`"active"` can never keep a lapsed member receiving mail. The one sticky
stored value is `cancelled` (set by `remove_member`); everything else --
`active`, `expired`, `legacy` (never expires, `expires_at: null`) -- is
derived. Writes go through `quantcheck.state.atomic_write_json` (tmp file +
`os.replace`) and a timestamped `.bak` is written before every save,
mirroring `quantcheck/recipients.py`'s existing backup convention.

## Filtering: how a membership actually stops mail

The filter lives in `quantcheck/notify_routes.py:subscriber_recipients()`,
applied after the existing `parse_recipients()` call. Because
`official_mail_forwarder.py` and `picks_check.py` both resolve recipients
through the same `PICKS_UPDATE` route, filtering in one place covers both
picks-update reports and forwarded official mail, per the decision that
expiry stops *both* channels together. `admin_recipients()` is never
filtered -- admins are operators, not subscribers.

**Fail-open is the load-bearing safety property here, not an edge case.**
Every one of these fails open (subscriber is treated as valid and mail is
NOT dropped), with a loud log line in `logs/notify_routes.log`:

- `state/memberships.json` is missing entirely.
- The file exists but is not valid JSON, or a member row is malformed.
- A subscriber's email is not found anywhere in the membership store
  (e.g. they predate the membership system).

The only way a subscriber is actually excluded is an *unambiguous* record
saying they are expired or cancelled. See
`tests/test_notify_routes_membership.py` (`StoreMissingOrCorruptFailOpenTests`,
`UnknownEmailFailOpenTests`) for the tests that pin this down, including one
at the real 81-subscriber scale.

### Kill switch

`MEMBERSHIP_ENFORCEMENT` (env var / `.env`), default `1`:

```env
MEMBERSHIP_ENFORCEMENT=0
```

Setting it to `0` disables membership filtering instantly and completely --
`subscriber_recipients()` returns the raw `notify_recipients.txt` contents
without even opening `state/memberships.json`. This is the "something's
wrong at 2am, turn it off" switch; it does not require the store to be
readable to work.

## The 2026-08-31 migration and the 2026-10-09 cliff

On 2026-08-31, all 81 pre-existing addresses in `notify_recipients.txt`
were migrated to the membership store in one batch:

```bash
quantcheck-admin members migrate --expires 2026-10-09 --dry-run   # reviewed first
quantcheck-admin members migrate --expires 2026-10-09 --note "migrated from notify_recipients.txt on 2026-08-31"
```

Every one of them got identical `status=active`, `expires_at=2026-10-09T00:00:00-04:00`,
`joined_at=<migration time>`, `months_total=0` (the migration doesn't know
how many months any individual subscriber actually paid for -- that has to
be corrected per-member with `quantcheck-admin members set-expiry` as real
payment history becomes known). The migration is idempotent: re-running it
only adds members not already present and never overwrites an existing
member's `expires_at`, so later manual corrections survive a re-run.
`notify_recipients.txt` was backed up first
(`notify_recipients.txt.20260831T123854Z.bak`) even though the migration
never modifies that file, only reads it.

**The risk this creates**: because everyone shares the same
`expires_at`, the instant `2026-10-09T00:00:00-04:00` arrives, every
subscriber who was not individually renewed before then gets excluded from
mail *simultaneously*. From 2026-08-31 to 2026-10-09 is about 5.5 weeks of
buffer to individually correct real expiry dates
(`quantcheck-admin members set-expiry EMAIL --date ... --note ...`) or
extend real renewals (`quantcheck-admin members extend EMAIL --months N`)
before the cliff.

Guardrails already in place for this specific risk (not optional, built in
from the start):

1. `MEMBERSHIP_ENFORCEMENT=0` kill switch (above).
2. Every `subscriber_recipients()` call logs `subscribers=N active=M
   excluded=K`, and lists the excluded addresses by name when `K > 0`.
3. The daily admin status email (`quantcheck/daily_admin_status.py`) has a
   membership section: active / expiring-within-7-days / expiring-within-14-days
   / expired counts, the actual list of who's expiring soon, and the current
   enforcement on/off state -- so the countdown to 10-09 is visible every
   single day, not discovered the hard way when subscribers stop getting mail.
4. Fail-open (above) means a broken membership store degrades to "everyone
   gets mail," never to "nobody gets mail."

## Day-to-day operations

```bash
quantcheck-admin members list [--status active] [--expiring-days 14]
quantcheck-admin members get EMAIL
quantcheck-admin members add EMAIL --months 1 [--note "..."] [--joined-at 2026-08-10]
quantcheck-admin members add EMAIL --expires-at 2026-11-01   # explicit expiry
quantcheck-admin members add EMAIL --align                    # same expiry as most active members
quantcheck-admin members extend EMAIL --months 3
quantcheck-admin members set-expiry EMAIL --date 2026-12-09 [--note "..."]
quantcheck-admin members set-expiry EMAIL --date null   # never expires (legacy)
quantcheck-admin members remove EMAIL --reason "..."
quantcheck-admin members sync                            # read-only drift report
quantcheck-admin route preview [--route picks_update]     # who actually gets mail right now
```

`add_member` and `extend_member` also keep `notify_recipients.txt` in sync
(via `quantcheck/recipients.py`'s existing file-writing functions, never
reimplemented) so a new member both exists in the membership store and is
in the actual mail list. `remove_member` marks the member `cancelled`
*and* takes them out of `notify_recipients.txt`, but keeps their record and
full history -- nothing is deleted.

Two intentional asymmetries worth knowing about:

- `extend_member` on a `cancelled` member flips them back to `active`
  (paying again is an unambiguous signal they should receive mail again).
- `set_expiry` never touches `status`, even on a cancelled member -- it is a
  pure date-correction tool. Use `extend_member` to reinstate someone.

### Adding a member with a given expiry (`--expires-at` / `--align`)

`add --months N` is anchor arithmetic: the first month ends at the *next*
9th, so adding someone on 2026-10-07 with `--months 1` yields a two-day
membership (expiry 2026-10-09). To put a new member on the same expiry as
everyone else, say so directly instead of adding and then correcting:

```bash
quantcheck-admin members add EMAIL --expires-at 2026-11-01
quantcheck-admin members add EMAIL --align     # most common expiry among active members
```

Exactly one of `--months`, `--expires-at`, `--align` is required. `--align`
uses the most common `expires_at` among currently-active members (ties go to
the later date) and fails with `no_active_members` when there are none. These
two modes leave `months_total` at `0` and write a history entry with
`"months": null`, `"expires_at": ...` and `"mode": "expires_at"` / `"align"`
(`"mode": "months"` for the classic path).

### Bulk operations

```bash
quantcheck-admin members bulk-add --file new.txt --align [--note "..."]            # dry-run
quantcheck-admin members bulk-add --file new.txt --align --apply                    # write
quantcheck-admin members bulk-extend a@x.com b@x.com --months 1 --apply
quantcheck-admin members bulk-extend --all-active --months 1                        # dry-run
quantcheck-admin members bulk-set-expiry --all-active --expires-at 2026-12-01 --apply
```

All bulk commands (CLI) and tools (`bulk_add_members`, `bulk_extend_members`,
`bulk_set_expiry`, MCP) default to **dry-run**; nothing is written until
`--apply` / `dry_run=false`. Input may be a messy paste (whitespace, commas,
semicolons, newlines; `#` starts a comment). Invalid addresses, existing
members (bulk-add) and unknown emails (bulk-extend / bulk-set-expiry) are
listed per email and skipped. An apply saves `memberships.json` once and
writes `notify_recipients.txt` at most once (via the same `recipients.py`
helpers). `--all-active` means currently-active only: legacy
(`expires_at: null`), expired and cancelled members are not touched.

### Payments

Payments are recorded inside history entries (`"payment": {"amount",
"currency", "channel", "paid_at", "ref"}`); there are no new top-level store
fields, so a `memberships.json` written by older code still loads, and
filtering in `notify_routes` (fail-open included) never looks at them.

```bash
quantcheck-admin members add EMAIL --align --amount 30 --currency CNY --channel wechat --ref ORDER123
quantcheck-admin members extend EMAIL --months 1 --amount 30 --currency CNY --channel alipay
quantcheck-admin members record-payment EMAIL --amount 30 --currency CNY --channel wechat --paid-at 2026-10-05
quantcheck-admin members payments --since 2026-10-01 --until 2026-10-31
```

`record-payment` only records money received; it does not change expiry,
status, `months_total` or the mail list (use `extend` to also grant time).
`members get` adds derived `payments` and `total_paid` (currency -> sum;
payments with no currency are summed under `"unspecified"`), and `members
payments` totals by currency and by channel. `--amount` on `bulk-add` is per
member.

See `docs/AGENT_API.md` for the full CLI/MCP reference, and
`docs/OPERATIONS.md` for how this fits into the rest of day-to-day
operations.
