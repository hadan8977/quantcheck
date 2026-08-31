"""Programmatic service layer for quantcheck.

This package is the single source of truth for membership CRUD and
operational tooling, shared by two thin front ends:

- `quantcheck.admin_cli` (`quantcheck-admin`): a JSON-by-default CLI meant
  for agents and scripts, with a `--human` table mode for people.
- `quantcheck.mcp_server` (`quantcheck-mcp`): an MCP stdio server exposing
  the same operations as tools.

Contract every public function in `quantcheck.service.members` and
`quantcheck.service.ops` follows:

- Returns a plain JSON-serializable dict. Never prints. Never calls
  `sys.exit` or raises `SystemExit`.
- Expected failures (bad input, missing record, unreadable storage) raise
  `quantcheck.service.errors.ServiceError`. Unexpected failures propagate as
  whatever exception they naturally are; callers should not swallow those
  silently.
- Real subscriber mail is never sent implicitly. Anything that can send
  mail requires an explicit `confirm=True` (see `ops.run_job`), and the
  actual historical-resend *send* path is deliberately not exposed here at
  all -- only its preview is (see `ops` for details).
"""

from __future__ import annotations

from quantcheck.service.errors import ServiceError

__all__ = ["ServiceError"]
