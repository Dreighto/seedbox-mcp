from __future__ import annotations

from typing import Any

from whatbox_media_mcp.errors import MediaMcpError
from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import (
    bool_params,
    clamp_limit,
    compact_queue_item,
    compact_series,
    safe_tool,
)

SONARR_RESEARCH_COMMANDS = {
    "series_search": "SeriesSearch",
    "refresh": "RefreshSeries",
    "missing_episode_search": "MissingEpisodeSearch",
}


async def sonarr_overview(
    services: Services,
    include_series: bool = True,
    include_queue: bool = True,
    include_missing: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit)
        data: dict[str, Any] = {"limit": bounded}
        if include_series:
            series = await services.sonarr.get("/api/v3/series")
            data["series"] = [compact_series(item) for item in _as_list(series)[:bounded]]
        if include_queue:
            queue = await services.sonarr.get("/api/v3/queue", {"page": 1, "pageSize": bounded})
            data["queue"] = [compact_queue_item("sonarr", item) for item in _records(queue)[:bounded]]
        if include_missing:
            missing = await services.sonarr.get("/api/v3/wanted/missing", {"page": 1, "pageSize": bounded})
            data["missing"] = [
                {
                    "sonarr_id": item.get("seriesId"),
                    "series_title": item.get("series", {}).get("title"),
                    "season_number": item.get("seasonNumber"),
                    "episode_number": item.get("episodeNumber"),
                    "title": item.get("title"),
                    "air_date_utc": item.get("airDateUtc"),
                }
                for item in _records(missing)[:bounded]
            ]
        return ToolResponse.success(data)

    return await safe_tool(run)


async def sonarr_add_series(
    services: Services,
    tvdb_id: int,
    title: str | None = None,
    quality_profile_id: int | None = None,
    root_folder: str | None = None,
    series_type: str | None = None,
    season_folder: bool = True,
    monitor: str = "future",
    search_now: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not tvdb_id:
            raise MediaMcpError("validation", "sonarr_add_series requires tvdb_id.")
        existing = await _find_existing_series(services, tvdb_id)
        if existing:
            return ToolResponse.success({"action": "already_exists", "series": compact_series(existing)})
        lookup = await services.sonarr.get("/api/v3/series/lookup", {"term": f"tvdb:{tvdb_id}"})
        candidates = _as_list(lookup)
        if not candidates:
            raise MediaMcpError("not_found", "Sonarr lookup did not return a series.", {"tvdb_id": tvdb_id})
        selected = candidates[0]
        payload = {
            **selected,
            "qualityProfileId": quality_profile_id or services.settings.sonarr_default_quality_profile_id,
            "rootFolderPath": root_folder or services.settings.sonarr_default_root_folder,
            "seriesType": series_type or services.settings.sonarr_default_series_type,
            "seasonFolder": season_folder,
            "monitored": monitor != "none",
            "addOptions": {"monitor": monitor, "searchForMissingEpisodes": search_now},
        }
        if services.settings.sonarr_default_language_profile_id is not None:
            payload["languageProfileId"] = services.settings.sonarr_default_language_profile_id
        preview = {
            "tvdb_id": tvdb_id,
            "title": title or selected.get("title"),
            "root_folder": payload["rootFolderPath"],
            "quality_profile_id": payload["qualityProfileId"],
            "series_type": payload["seriesType"],
            "monitor": monitor,
            "search_now": search_now,
        }
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_add": preview})
        created = await services.sonarr.post("/api/v3/series", payload)
        return ToolResponse.success({"dry_run": False, "created": compact_series(created)})

    return await safe_tool(run)


async def sonarr_delete_series(
    services: Services,
    sonarr_id: int,
    delete_files: bool = False,
    add_import_exclusion: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not sonarr_id:
            raise MediaMcpError("validation", "sonarr_delete_series requires sonarr_id.")
        series = await services.sonarr.get(f"/api/v3/series/{sonarr_id}")
        if not isinstance(series, dict):
            raise MediaMcpError("not_found", "Sonarr series was not found.", {"sonarr_id": sonarr_id})
        preview = {
            "sonarr_id": sonarr_id,
            "title": series.get("title"),
            "path": series.get("path"),
            "monitored": series.get("monitored"),
            "delete_files": delete_files,
            "add_import_exclusion": add_import_exclusion,
        }
        warnings = ["delete_files=true will ask Sonarr to delete media files."] if delete_files else []
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_delete": preview}, warnings)
        await services.sonarr.delete(
            f"/api/v3/series/{sonarr_id}",
            bool_params({"deleteFiles": delete_files, "addImportListExclusion": add_import_exclusion}),
        )
        return ToolResponse.success({"dry_run": False, "deleted": preview}, warnings)

    return await safe_tool(run)


async def sonarr_research_series(
    services: Services,
    sonarr_id: int,
    mode: str,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not sonarr_id:
            raise MediaMcpError("validation", "sonarr_research_series requires sonarr_id.")
        command_name = SONARR_RESEARCH_COMMANDS.get(mode)
        if not command_name:
            raise MediaMcpError(
                "validation",
                "Unsupported Sonarr research mode.",
                {"allowed": sorted(SONARR_RESEARCH_COMMANDS)},
            )
        series = await services.sonarr.get(f"/api/v3/series/{sonarr_id}")
        payload = {"name": command_name, "seriesId": sonarr_id}
        preview = {"series": compact_series(series) if isinstance(series, dict) else None, "command": payload}
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_run": preview})
        command = await services.sonarr.post("/api/v3/command", payload)
        return ToolResponse.success({"dry_run": False, "command": command})

    return await safe_tool(run)


async def _find_existing_series(services: Services, tvdb_id: int) -> dict[str, Any] | None:
    series = await services.sonarr.get("/api/v3/series")
    for item in _as_list(series):
        if item.get("tvdbId") == tvdb_id:
            return item
    return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return _as_list(value.get("records", []))
    return _as_list(value)
