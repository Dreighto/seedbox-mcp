from __future__ import annotations

from typing import Any, Literal

ErrorType = Literal[
    "not_found",
    "ambiguous",
    "upstream_unreachable",
    "upstream_auth",
    "validation",
    "unsafe_request",
    "unsupported",
]


class MediaMcpError(Exception):
    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.details = details or {}


class UpstreamError(MediaMcpError):
    pass
