from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import (
    clamp_limit,
    compact_movie,
    compact_plex_item,
    compact_queue_item,
    compact_series,
    safe_tool,
)

VALID_SORTS = {"staleness_desc", "size_desc", "title_asc"}
PLEX_PAGE_SIZE = 500


async def staleness_report(
    services: Services,
    media_type: str = "all",
    older_than_days: int = 90,
    include_unwatched: bool = True,
    include_unmanaged: bool = True,
    include_missing: bool = True,
    limit: int = 100,
    sort: str = "staleness_desc",
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if sort not in VALID_SORTS:
            return ToolResponse.failure(
                "validation",
                "Unsupported sort.",
                {"allowed": sorted(VALID_SORTS)},
            )
        bounded = clamp_limit(limit)
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        sections = await _sections(services, media_type)
        plex_items = []
        for section in sections:
            plex_items.extend(await _get_all_plex_items(services, section))

        radarr_movies = _as_list(await services.radarr.get("/api/v3/movie"))
        sonarr_series = _as_list(await services.sonarr.get("/api/v3/series"))
        radarr_queue = await services.radarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})
        sonarr_queue = await services.sonarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})

        radarr_by_key = {
            (str(m.get("title", "")).casefold(), m.get("year")): m.get("id")
            for m in radarr_movies
            if m.get("title") and m.get("id")
        }
        sonarr_by_key = {
            (str(s.get("title", "")).casefold(), s.get("year")): s.get("id")
            for s in sonarr_series
            if s.get("title") and s.get("id")
        }

        data: dict[str, Any] = {"older_than_days": older_than_days, "limit": bounded, "sort": sort}
        if include_unwatched:
            unwatched_raw = [
                item
                for item in plex_items
                if not item.get("view_count")
                and not item.get("last_viewed_at")
                and _before(item.get("added_at"), cutoff)
            ]
            data["added_long_ago_unwatched"] = _sort_and_slice(
                [_annotate(item, radarr_by_key, sonarr_by_key) for item in unwatched_raw],
                sort,
                bounded,
            )
            watched_raw = [
                item
                for item in plex_items
                if item.get("last_viewed_at") and _before(item.get("last_viewed_at"), cutoff)
            ]
            data["watched_long_ago"] = _sort_and_slice(
                [_annotate(item, radarr_by_key, sonarr_by_key) for item in watched_raw],
                sort,
                bounded,
            )
        if include_unmanaged:
            managed_titles = {str(item.get("title", "")).casefold() for item in [*radarr_movies, *sonarr_series]}
            unmanaged_raw = [item for item in plex_items if str(item.get("title", "")).casefold() not in managed_titles]
            data["plex_unmanaged"] = _sort_and_slice(
                [compact_plex_item(item) for item in unmanaged_raw],
                sort,
                bounded,
            )
        if include_missing:
            plex_titles = {str(item.get("title", "")).casefold() for item in plex_items}
            plex_file_paths = {path for item in plex_items for path in item.get("file_paths") or []}
            missing: list[dict[str, Any]] = []
            if media_type in ("movies", "all"):
                missing += [
                    compact_movie(item)
                    for item in radarr_movies
                    if not item.get("hasFile") or not _movie_file_in_plex(item, plex_file_paths)
                ]
            if media_type in ("tv", "all"):
                missing += [
                    compact_series(item)
                    for item in sonarr_series
                    if str(item.get("title", "")).casefold() not in plex_titles
                ]
            data["managed_missing_from_plex"] = missing[:bounded]
        data["queue_warnings"] = [
            compact_queue_item("radarr", item) for item in _records(radarr_queue) if _stuck(item)
        ][:bounded] + [compact_queue_item("sonarr", item) for item in _records(sonarr_queue) if _stuck(item)][:bounded]
        return ToolResponse.success(data)

    return await safe_tool(run)


async def _get_all_plex_items(services: Services, section: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await services.plex.get_basic_library_items(section, PLEX_PAGE_SIZE, offset)
        items.extend(page)
        if len(page) < PLEX_PAGE_SIZE:
            return items
        offset += len(page)


def _annotate(
    item: dict[str, Any],
    radarr_by_key: dict[tuple[str, Any], Any],
    sonarr_by_key: dict[tuple[str, Any], Any],
) -> dict[str, Any]:
    compact = compact_plex_item(item)
    key = (str(compact.get("title", "")).casefold(), compact.get("year"))
    if compact.get("type") == "show":
        sonarr_id = sonarr_by_key.get(key)
        compact["sonarr_id"] = sonarr_id
        compact["match_status"] = "matched" if sonarr_id else "unmanaged"
    else:
        radarr_id = radarr_by_key.get(key)
        compact["radarr_id"] = radarr_id
        compact["match_status"] = "matched" if radarr_id else "unmanaged"
    return compact


def _sort_and_slice(items: list[dict[str, Any]], sort: str, limit: int) -> list[dict[str, Any]]:
    if sort == "size_desc":
        items.sort(key=lambda i: i.get("size_on_disk_gb") or -1.0, reverse=True)
    elif sort == "title_asc":
        items.sort(key=lambda i: (i.get("title") or "").casefold())
    else:  # staleness_desc — oldest most-recent-activity first
        items.sort(key=_staleness_sort_key)
    return items[:limit]


def _staleness_sort_key(item: dict[str, Any]) -> tuple[int, float]:
    added = _parse_iso(item.get("added_at"))
    viewed = _parse_iso(item.get("last_viewed_at"))
    timestamps = [t for t in (added, viewed) if t is not None]
    if not timestamps:
        # Items with no timestamps sort last (group 1).
        return (1, 0.0)
    return (0, max(timestamps))


def _parse_iso(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


async def _sections(services: Services, media_type: str) -> list[str]:
    # Discover every Plex library of the relevant type rather than only the
    # single configured display name. A movie can live in "Movies",
    # "Anime Movies", or "4K Movies" (all Plex type "movie"); reconciling
    # Radarr/Sonarr state against just one of them falsely reports the rest
    # as missing from Plex. Fall back to the configured names if discovery
    # fails so the report still runs when Plex metadata is unavailable.
    try:
        by_type = await services.plex.get_sections_by_type()
    except Exception:
        by_type = {}
    movie_sections = by_type.get("movie") or [services.settings.plex_movie_section]
    show_sections = by_type.get("show") or [services.settings.plex_tv_section]
    if media_type == "movies":
        return list(dict.fromkeys(movie_sections))
    if media_type == "tv":
        return list(dict.fromkeys(show_sections))
    return list(dict.fromkeys([*movie_sections, *show_sections]))


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


def _movie_file_in_plex(movie: dict[str, Any], plex_file_paths: set[str]) -> bool:
    # Match on file path, not title: Plex often displays a different matched
    # title than Radarr's (e.g. "Alien: Resurrection" vs "Alien Resurrection",
    # US vs UK Harry Potter subtitles), which made title-string comparison
    # flag correctly-indexed movies as missing.
    movie_file = movie.get("movieFile") or {}
    path = movie_file.get("path")
    return bool(path) and path in plex_file_paths


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return _as_list(value.get("records", []))
    return _as_list(value)
