from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from whatbox_media_mcp.errors import MediaMcpError


class ApiError(BaseModel):
    error_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return cls(ok=True, data=data or {}, warnings=warnings or []).model_dump(exclude_none=True)

    @classmethod
    def failure(
        cls,
        error_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return cls(
            ok=False,
            error_type=error_type,
            message=message,
            details=details or {},
        ).model_dump(exclude_none=True)

    @classmethod
    def from_error(cls, error: MediaMcpError) -> dict[str, Any]:
        return cls.failure(error.error_type, error.message, error.details)


class MediaReference(BaseModel):
    kind: Literal["movie", "series", "plex_item"]
    source: str
    title: str
    year: int | None = None
    exists: bool = False
    confidence: float = 0.0
    radarr_id: int | None = None
    sonarr_id: int | None = None
    plex_rating_key: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None


class QueueItemSummary(BaseModel):
    source: Literal["radarr", "sonarr"]
    title: str
    status: str
    tracked_download_state: str | None = None
    progress_percent: float | None = None
    estimated_completion_time: str | None = None
    error_message: str | None = None


class PlexItemSummary(BaseModel):
    type: str
    title: str
    year: int | None = None
    section: str
    rating_key: str
    added_at: str | None = None
    last_viewed_at: str | None = None
    view_count: int | None = None
    duration_minutes: int | None = None
    file_paths: list[str] = Field(default_factory=list)
