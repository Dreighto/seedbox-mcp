from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from rapidfuzz import fuzz

from seedbox_mcp.errors import MediaMcpError
from seedbox_mcp.schemas import ToolResponse

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
        detail = exc.details.get("detail") if exc.details else None
        return None, f"{exc.message} ({detail})" if detail else exc.message


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
    # The queue record's top-level `title` is the raw release name (scene string),
    # not the media title. The clean title and the media id live on the embedded
    # movie/series object, which the overview fetch requests via includeMovie/
    # includeSeries. Surfacing both lets an agent go queue -> radarr_id/sonarr_id ->
    # research/queue_action directly, instead of fuzzy-matching the release name.
    movie = item.get("movie") or {}
    series = item.get("series") or {}
    compact = {
        "queue_id": item.get("id"),
        "source": source,
        "title": movie.get("title") or series.get("title") or pick_title(item),
        "release_title": item.get("title"),
        "status": item.get("status") or "unknown",
        "tracked_download_state": tracked or "unknown",
        "progress_percent": progress_percent,
        "estimated_completion_time": item.get("estimatedCompletionTime"),
        "error_message": item.get("errorMessage") or item.get("statusMessages"),
    }
    if source == "radarr":
        compact["radarr_id"] = item.get("movieId") or movie.get("id")
    else:
        compact["sonarr_id"] = item.get("seriesId") or series.get("id")
    return compact


def bytes_to_gb(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(value / 1024**3, 2)


def compact_movie(item: dict[str, Any]) -> dict[str, Any]:
    movie_file = item.get("movieFile") or {}
    quality = ((movie_file.get("quality") or {}).get("quality") or {}).get("name")
    return {
        "radarr_id": item.get("id"),
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdb_id": item.get("tmdbId"),
        "imdb_id": item.get("imdbId"),
        "monitored": item.get("monitored"),
        "has_file": item.get("hasFile"),
        "quality": quality,
        "size_on_disk_gb": bytes_to_gb(item.get("sizeOnDisk")),
        "path": item.get("path"),
    }


def compact_series(item: dict[str, Any], include_seasons: bool = False) -> dict[str, Any]:
    stats = item.get("statistics") or {}
    out = {
        "sonarr_id": item.get("id"),
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdb_id": item.get("tvdbId"),
        "imdb_id": item.get("imdbId"),
        "monitored": item.get("monitored"),
        "episode_file_count": stats.get("episodeFileCount"),
        "episode_count": stats.get("episodeCount"),
        "size_on_disk_gb": bytes_to_gb(stats.get("sizeOnDisk")),
        "next_airing": item.get("nextAiring"),
        "path": item.get("path"),
    }
    # Seasons are opt-in: only the series-listing read needs them. Delete/queue/add
    # previews reuse this projection and would only be bloated by the per-season array.
    if include_seasons:
        out["seasons"] = [compact_season(s) for s in item.get("seasons") or [] if isinstance(s, dict)]
    return out


def compact_season(season: dict[str, Any]) -> dict[str, Any]:
    stats = season.get("statistics") or {}
    return {
        "season_number": season.get("seasonNumber"),
        "monitored": season.get("monitored"),
        "episode_file_count": stats.get("episodeFileCount"),
        "episode_count": stats.get("episodeCount"),
        "size_on_disk_gb": bytes_to_gb(stats.get("sizeOnDisk")),
    }


# Fields kept in compact Plex item projections used by list-context tools
# (staleness_report, plex_overview recently_added / basic_staleness_candidates).
# Dropped vs the full PlexClient._summarize_item shape: file_paths, directors,
# duration_minutes — none of those are decision-relevant for cleanup or
# what's-new lists, and they bloat responses.
_COMPACT_PLEX_FIELDS = (
    "type",
    "title",
    "year",
    "section",
    "rating_key",
    "added_at",
    "last_viewed_at",
    "view_count",
    "size_on_disk_gb",
)


def compact_plex_item(item: dict[str, Any], full: bool = False) -> dict[str, Any]:
    if full:
        return dict(item)
    return {key: item.get(key) for key in _COMPACT_PLEX_FIELDS}


# Top-level aggregates kept from Tautulli get_activity in overview contexts.
_COMPACT_TAUTULLI_ACTIVITY_FIELDS = (
    "stream_count",
    "stream_count_direct_play",
    "stream_count_direct_stream",
    "stream_count_transcode",
    "total_bandwidth",
    "lan_bandwidth",
    "wan_bandwidth",
)

# Per-session fields kept: identity, who/what, and the transcode-decision
# shape. Drops the full video_*/audio_*/subtitle_*/stream_*/transcode_hw_*
# detail, file paths, credits/genres, IP addresses and email — none of which
# inform agent decisions in an overview.
_COMPACT_TAUTULLI_SESSION_FIELDS = (
    "session_key",
    "media_type",
    "title",
    "parent_title",
    "grandparent_title",
    "year",
    "user",
    "state",
    "progress_percent",
    "view_offset",
    "duration",
    "rating_key",
    "library_name",
    "player",
    "platform",
    "product",
    "location",
    "bandwidth",
    "quality_profile",
    "transcode_decision",
    "video_decision",
    "audio_decision",
    "subtitle_decision",
)


def compact_tautulli_activity(activity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(activity, dict):
        return {}
    result: dict[str, Any] = {key: activity.get(key) for key in _COMPACT_TAUTULLI_ACTIVITY_FIELDS}
    sessions = activity.get("sessions") or []
    result["sessions"] = [_compact_tautulli_session(session) for session in sessions if isinstance(session, dict)]
    return result


def _compact_tautulli_session(session: dict[str, Any]) -> dict[str, Any]:
    result = {key: session.get(key) for key in _COMPACT_TAUTULLI_SESSION_FIELDS}
    return {k: v for k, v in result.items() if v not in (None, "")}


def sum_size_gb(items: list[dict[str, Any]]) -> float:
    total = sum(item.get("size_on_disk_gb") or 0.0 for item in items)
    return round(float(total), 2)


def is_exact_title_year_match(
    query: str | None,
    query_year: int | None,
    title: str | None,
    item_year: int | None,
) -> tuple[bool, str]:
    """Return (safe_for_action, match_type) for a search candidate.

    safe_for_action is True only for exact_title_year, or exact_title when
    the query did not supply a year (so the agent cannot accidentally pick
    the wrong-year duplicate). Anything else is "fuzzy".
    """
    if not query or not title:
        return False, "fuzzy"
    if query.strip().casefold() != title.strip().casefold():
        return False, "fuzzy"
    if query_year is not None:
        if item_year == query_year:
            return True, "exact_title_year"
        return False, "fuzzy"
    return True, "exact_title"
