from __future__ import annotations

from typing import Any

from whatbox_media_mcp.errors import MediaMcpError
from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import (
    bool_params,
    clamp_limit,
    compact_movie,
    compact_queue_item,
    pick_title,
    safe_tool,
)

RADARR_QUEUE_ACTIONS = {"remove", "blocklist"}

RADARR_RESEARCH_COMMANDS = {
    "search": "MoviesSearch",
    "refresh": "RefreshMovie",
    "scan_downloaded": "DownloadedMoviesScan",
}


async def radarr_overview(
    services: Services,
    include_movies: bool = True,
    include_queue: bool = True,
    include_missing: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit)
        data: dict[str, Any] = {"limit": bounded}
        if include_movies:
            movies = await services.radarr.get("/api/v3/movie")
            data["movies"] = [compact_movie(item) for item in _as_list(movies)[:bounded]]
        if include_queue:
            queue = await services.radarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})
            data["queue"] = [compact_queue_item("radarr", item) for item in _records(queue)[:bounded]]
        if include_missing:
            missing = await services.radarr.get("/api/v3/wanted/missing", {"page": 1, "pageSize": bounded})
            data["missing"] = [compact_movie(item.get("movie", item)) for item in _records(missing)[:bounded]]
        return ToolResponse.success(data)

    return await safe_tool(run)


async def radarr_add_movie(
    services: Services,
    tmdb_id: int,
    title: str | None = None,
    year: int | None = None,
    quality_profile_id: int | None = None,
    root_folder: str | None = None,
    minimum_availability: str | None = None,
    monitored: bool = True,
    search_now: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not tmdb_id:
            raise MediaMcpError("validation", "radarr_add_movie requires tmdb_id.")
        existing = await _find_existing_movie(services, tmdb_id)
        if existing:
            return ToolResponse.success({"action": "already_exists", "movie": compact_movie(existing)})
        lookup = await services.radarr.get("/api/v3/movie/lookup/tmdb", {"tmdbId": tmdb_id})
        if not isinstance(lookup, dict):
            raise MediaMcpError("not_found", "Radarr lookup did not return a movie.", {"tmdb_id": tmdb_id})
        payload = {
            **lookup,
            "qualityProfileId": quality_profile_id or services.settings.radarr_default_quality_profile_id,
            "rootFolderPath": root_folder or services.settings.radarr_default_root_folder,
            "minimumAvailability": minimum_availability or services.settings.radarr_default_min_availability,
            "monitored": monitored,
            "addOptions": {"searchForMovie": search_now},
        }
        preview = {
            "tmdb_id": tmdb_id,
            "title": title or lookup.get("title"),
            "year": year or lookup.get("year"),
            "root_folder": payload["rootFolderPath"],
            "quality_profile_id": payload["qualityProfileId"],
            "minimum_availability": payload["minimumAvailability"],
            "search_now": search_now,
        }
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_add": preview})
        created = await services.radarr.post("/api/v3/movie", payload)
        return ToolResponse.success({"dry_run": False, "created": compact_movie(created)})

    return await safe_tool(run)


async def radarr_delete_movie(
    services: Services,
    radarr_id: int,
    delete_files: bool = False,
    add_import_exclusion: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not radarr_id:
            raise MediaMcpError("validation", "radarr_delete_movie requires radarr_id.")
        movie = await services.radarr.get(f"/api/v3/movie/{radarr_id}")
        if not isinstance(movie, dict):
            raise MediaMcpError("not_found", "Radarr movie was not found.", {"radarr_id": radarr_id})
        preview = {
            "radarr_id": radarr_id,
            "title": movie.get("title"),
            "year": movie.get("year"),
            "path": movie.get("path"),
            "monitored": movie.get("monitored"),
            "delete_files": delete_files,
            "add_import_exclusion": add_import_exclusion,
        }
        warnings = ["delete_files=true will ask Radarr to delete media files."] if delete_files else []
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_delete": preview}, warnings)
        await services.radarr.delete(
            f"/api/v3/movie/{radarr_id}",
            bool_params({"deleteFiles": delete_files, "addImportExclusion": add_import_exclusion}),
        )
        return ToolResponse.success({"dry_run": False, "deleted": preview}, warnings)

    return await safe_tool(run)


async def radarr_research_movie(
    services: Services,
    radarr_id: int,
    mode: str,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not radarr_id:
            raise MediaMcpError("validation", "radarr_research_movie requires radarr_id.")
        command_name = RADARR_RESEARCH_COMMANDS.get(mode)
        if not command_name:
            raise MediaMcpError(
                "validation",
                "Unsupported Radarr research mode.",
                {"allowed": sorted(RADARR_RESEARCH_COMMANDS)},
            )
        movie = await services.radarr.get(f"/api/v3/movie/{radarr_id}")
        payload = (
            {"name": command_name} if mode == "scan_downloaded" else {"name": command_name, "movieIds": [radarr_id]}
        )
        preview = {"movie": compact_movie(movie) if isinstance(movie, dict) else None, "command": payload}
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_run": preview})
        command = await services.radarr.post("/api/v3/command", payload)
        return ToolResponse.success({"dry_run": False, "command": command})

    return await safe_tool(run)


async def radarr_queue_action(
    services: Services,
    queue_id: int,
    action: str,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not queue_id:
            raise MediaMcpError("validation", "radarr_queue_action requires queue_id.")
        if action not in RADARR_QUEUE_ACTIONS:
            raise MediaMcpError("validation", "Unsupported action.", {"allowed": sorted(RADARR_QUEUE_ACTIONS)})
        queue = await services.radarr.get("/api/v3/queue", {"page": 1, "pageSize": 250})
        item = next((i for i in _records(queue) if i.get("id") == queue_id), None)
        if not item:
            raise MediaMcpError("not_found", "Queue item not found.", {"queue_id": queue_id})
        blocklist = action == "blocklist"
        preview = {
            "queue_id": queue_id,
            "title": pick_title(item),
            "status": item.get("status"),
            "tracked_download_state": item.get("trackedDownloadState") or item.get("trackedDownloadStatus"),
            "action": action,
            "remove_from_client": False,
            "blocklist": blocklist,
        }
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_action": preview})
        await services.radarr.delete(
            f"/api/v3/queue/{queue_id}",
            bool_params({"removeFromClient": False, "blocklist": blocklist}),
        )
        return ToolResponse.success({"dry_run": False, "actioned": preview})

    return await safe_tool(run)


async def _find_existing_movie(services: Services, tmdb_id: int) -> dict[str, Any] | None:
    movies = await services.radarr.get("/api/v3/movie")
    for movie in _as_list(movies):
        if movie.get("tmdbId") == tmdb_id:
            return movie
    return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return _as_list(value.get("records", []))
    return _as_list(value)
