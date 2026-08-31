"""Shared error type for the quantcheck service layer.

Every function in `quantcheck.service.members` and `quantcheck.service.ops`
raises `ServiceError` for expected failures (bad input, missing record,
storage trouble) instead of printing a message and calling `sys.exit`, so
both the JSON CLI (`quantcheck-admin`) and the MCP server can turn it into
their own error envelope without scraping stdout or catching bare
`Exception`.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message
