from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import (
    clamp_limit,
    compact_movie,
    compact_queue_item,
    compact_series,
    safe_tool,
)


async def staleness_report(
    services: Services,
    media_type: str = "all",
    older_than_days: int = 90,
    include_unwatched: bool = True,
    include_unmanaged: bool = True,
    include_missing: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit)
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        sections = _sections(services, media_type)
        plex_items = []
        for section in sections:
            plex_items.extend(await services.plex.get_basic_library_items(section, bounded))

        radarr_movies = _as_list(await services.radarr.get("/api/v3/movie"))
        sonarr_series = _as_list(await services.sonarr.get("/api/v3/series"))
        radarr_queue = await services.radarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})
        sonarr_queue = await services.sonarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})

        data: dict[str, Any] = {"older_than_days": older_than_days, "limit": bounded}
        if include_unwatched:
            data["added_long_ago_unwatched"] = [
                item for item in plex_items if not item.get("view_count") and _before(item.get("added_at"), cutoff)
            ][:bounded]
            data["watched_long_ago"] = [
                item
                for item in plex_items
                if item.get("last_viewed_at") and _before(item.get("last_viewed_at"), cutoff)
            ][:bounded]
        if include_unmanaged:
            managed_titles = {str(item.get("title", "")).lower() for item in [*radarr_movies, *sonarr_series]}
            data["plex_unmanaged"] = [
                item for item in plex_items if str(item.get("title", "")).lower() not in managed_titles
            ][:bounded]
        if include_missing:
            plex_titles = {str(item.get("title", "")).lower() for item in plex_items}
            data["managed_missing_from_plex"] = [
                compact_movie(item)
                for item in radarr_movies
                if not item.get("hasFile") or str(item.get("title", "")).lower() not in plex_titles
            ][:bounded] + [
                compact_series(item) for item in sonarr_series if str(item.get("title", "")).lower() not in plex_titles
            ][:bounded]
        data["queue_warnings"] = [
            compact_queue_item("radarr", item) for item in _records(radarr_queue) if _stuck(item)
        ][:bounded] + [compact_queue_item("sonarr", item) for item in _records(sonarr_queue) if _stuck(item)][:bounded]
        return ToolResponse.success(data)

    return await safe_tool(run)


def _sections(services: Services, media_type: str) -> list[str]:
    if media_type == "movies":
        return [services.settings.plex_movie_section]
    if media_type == "tv":
        return [services.settings.plex_tv_section]
    return [services.settings.plex_movie_section, services.settings.plex_tv_section]


def _before(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < cutoff


def _stuck(item: dict[str, Any]) -> bool:
    state = str(item.get("trackedDownloadState") or item.get("trackedDownloadStatus") or "").lower()
    status = str(item.get("status") or "").lower()
    return any(marker in state or marker in status for marker in ["warning", "failed", "blocked", "pending"])


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return _as_list(value.get("records", []))
    return _as_list(value)
