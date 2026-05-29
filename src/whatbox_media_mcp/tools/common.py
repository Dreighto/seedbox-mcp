from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from rapidfuzz import fuzz

from whatbox_media_mcp.errors import MediaMcpError
from whatbox_media_mcp.schemas import ToolResponse

T = TypeVar("T")


async def safe_tool(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await call()
    except MediaMcpError as exc:
        return ToolResponse.from_error(exc)


async def partial_call(call: Callable[[], Awaitable[T]]) -> tuple[T | None, str | None]:
    try:
        return await call(), None
    except MediaMcpError as exc:
        return None, exc.message


def clamp_limit(limit: int, default: int = 100, maximum: int = 500) -> int:
    if limit <= 0:
        return default
    return min(limit, maximum)


def bool_params(params: dict[str, bool]) -> dict[str, str]:
    return {key: str(value).lower() for key, value in params.items()}


def pick_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("series", {}).get("title") or item.get("movie", {}).get("title") or "")


def confidence(query: str, title: str, year: int | None = None) -> float:
    score = fuzz.WRatio(query.lower(), title.lower()) / 100
    if year and str(year) in query:
        score = min(1.0, score + 0.05)
    return round(score, 3)


def compact_queue_item(source: str, item: dict[str, Any]) -> dict[str, Any]:
    tracked = item.get("trackedDownloadState") or item.get("trackedDownloadStatus")
    progress = item.get("sizeleft")
    total = item.get("size")
    progress_percent = None
    if isinstance(progress, (int, float)) and isinstance(total, (int, float)) and total:
        progress_percent = round(max(0.0, min(100.0, 100 - (progress / total * 100))), 1)
    return {
        "source": source,
        "title": pick_title(item),
        "status": item.get("status") or "unknown",
        "tracked_download_state": tracked or "unknown",
        "progress_percent": progress_percent,
        "estimated_completion_time": item.get("estimatedCompletionTime"),
        "error_message": item.get("errorMessage") or item.get("statusMessages"),
    }


def compact_movie(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "radarr_id": item.get("id"),
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdb_id": item.get("tmdbId"),
        "imdb_id": item.get("imdbId"),
        "monitored": item.get("monitored"),
        "has_file": item.get("hasFile"),
        "path": item.get("path"),
    }


def compact_series(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("statistics") or {}
    return {
        "sonarr_id": item.get("id"),
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdb_id": item.get("tvdbId"),
        "imdb_id": item.get("imdbId"),
        "monitored": item.get("monitored"),
        "episode_file_count": stats.get("episodeFileCount"),
        "episode_count": stats.get("episodeCount"),
        "path": item.get("path"),
    }
