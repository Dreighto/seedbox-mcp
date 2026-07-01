from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from seedbox_mcp.errors import MediaMcpError
from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import (
    bool_params,
    bytes_to_gb,
    clamp_limit,
    compact_queue_item,
    compact_series,
    pick_title,
    safe_tool,
    sum_size_gb,
)

SONARR_QUEUE_ACTIONS = {"remove", "blocklist"}

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
    include_seasons: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit)
        data: dict[str, Any] = {"limit": bounded}
        if include_series:
            series = await services.sonarr.get("/api/v3/series")
            data["series"] = [
                compact_series(item, include_seasons=include_seasons) for item in _as_list(series)[:bounded]
            ]
        if include_queue:
            queue = await services.sonarr.get(
                "/api/v3/queue",
                {"page": 1, "pageSize": bounded, "includeSeries": True, "includeEpisode": True},
            )
            data["queue"] = [compact_queue_item("sonarr", item) for item in _records(queue)[:bounded]]
        if include_missing:
            missing = await services.sonarr.get("/api/v3/wanted/missing", {"page": 1, "pageSize": bounded})
            # Build id->title map if we didn't already fetch series, so titles aren't null
            series_titles: dict[int, str] = {}
            if include_series and "series" in data:
                series_titles = {s["sonarr_id"]: s["title"] for s in data["series"] if "sonarr_id" in s}
            else:
                raw_series = await services.sonarr.get("/api/v3/series")
                series_titles = {
                    item["id"]: item["title"] for item in _as_list(raw_series) if "id" in item and "title" in item
                }
            data["missing"] = [
                {
                    "sonarr_id": item.get("seriesId"),
                    "series_title": (
                        item.get("series", {}).get("title")
                        or item.get("seriesTitle")
                        or series_titles.get(int(item["seriesId"]) if "seriesId" in item else -1)
                    ),
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


async def sonarr_monitor_season(
    services: Services,
    sonarr_id: int,
    season_number: int,
    search_now: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not sonarr_id:
            raise MediaMcpError("validation", "sonarr_monitor_season requires sonarr_id.")
        if season_number is None or season_number < 0:
            raise MediaMcpError("validation", "sonarr_monitor_season requires a non-negative season_number.")
        series = await services.sonarr.get(f"/api/v3/series/{sonarr_id}")
        if not isinstance(series, dict):
            raise MediaMcpError("not_found", "Sonarr series was not found.", {"sonarr_id": sonarr_id})
        seasons = [s for s in series.get("seasons") or [] if isinstance(s, dict)]
        target = next((s for s in seasons if s.get("seasonNumber") == season_number), None)
        if target is None:
            available = sorted(s["seasonNumber"] for s in seasons if isinstance(s.get("seasonNumber"), int))
            raise MediaMcpError(
                "not_found",
                "Series has no such season.",
                {"sonarr_id": sonarr_id, "season_number": season_number, "available_seasons": available},
            )
        preview = {
            "sonarr_id": sonarr_id,
            "title": series.get("title"),
            "season_number": season_number,
            "already_monitored": bool(target.get("monitored")) and bool(series.get("monitored")),
            "search_now": search_now,
        }
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_monitor": preview})
        # Flip the target season — and ensure the series itself is monitored, since Sonarr
        # ignores season-level monitoring on an unmonitored series. There is no season-only
        # PATCH; the web UI round-trips the whole series object, so we PUT it back wholesale.
        target["monitored"] = True
        series["monitored"] = True
        updated = await services.sonarr.put(f"/api/v3/series/{sonarr_id}", series)
        command = None
        if search_now:
            command = await services.sonarr.post(
                "/api/v3/command",
                {"name": "SeasonSearch", "seriesId": sonarr_id, "seasonNumber": season_number},
            )
        return ToolResponse.success(
            {
                "dry_run": False,
                "monitored": preview,
                "series": compact_series(updated, include_seasons=True) if isinstance(updated, dict) else None,
                "search_command": command,
            }
        )

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
        stats = series.get("statistics") or {}
        preview = {
            "sonarr_id": sonarr_id,
            "title": series.get("title"),
            "path": series.get("path"),
            "monitored": series.get("monitored"),
            "size_on_disk_gb": bytes_to_gb(stats.get("sizeOnDisk")),
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


async def sonarr_delete_series_batch(
    services: Services,
    sonarr_ids: list[int],
    delete_files: bool = True,
    add_import_exclusion: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not sonarr_ids:
            raise MediaMcpError("validation", "sonarr_delete_series_batch requires a non-empty sonarr_ids list.")
        if any(not isinstance(sid, int) or sid <= 0 for sid in sonarr_ids):
            raise MediaMcpError(
                "validation",
                "sonarr_delete_series_batch sonarr_ids must be positive integers.",
            )

        series_list = _as_list(await services.sonarr.get("/api/v3/series"))
        by_id = {int(s["id"]): s for s in series_list if "id" in s}

        previews: list[dict[str, Any]] = []
        not_found: list[int] = []
        for sid in sonarr_ids:
            series = by_id.get(sid)
            if series is None:
                not_found.append(sid)
                continue
            stats = series.get("statistics") or {}
            previews.append(
                {
                    "sonarr_id": sid,
                    "title": series.get("title"),
                    "year": series.get("year"),
                    "path": series.get("path"),
                    "size_on_disk_gb": bytes_to_gb(stats.get("sizeOnDisk")),
                }
            )

        warnings: list[str] = []
        if delete_files:
            warnings.append("delete_files=true asks Sonarr to delete media files for all selected items.")

        if not confirm:
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_delete": previews,
                    "not_found": not_found,
                    "delete_files": delete_files,
                    "add_import_exclusion": add_import_exclusion,
                    "summary": {
                        "requested": len(sonarr_ids),
                        "found": len(previews),
                        "not_found": len(not_found),
                        "estimated_size_gb": sum_size_gb(previews),
                    },
                },
                warnings,
            )

        params = bool_params({"deleteFiles": delete_files, "addImportListExclusion": add_import_exclusion})
        deleted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = [{"sonarr_id": sid, "error_type": "not_found"} for sid in not_found]

        for preview in previews:
            sid = int(preview["sonarr_id"])
            try:
                await services.sonarr.delete(f"/api/v3/series/{sid}", params)
                deleted.append(preview)
            except MediaMcpError as exc:
                failed.append(
                    {
                        "sonarr_id": sid,
                        "title": preview.get("title"),
                        "error_type": exc.error_type,
                        "message": exc.message,
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "sonarr_id": sid,
                        "title": preview.get("title"),
                        "error_type": "upstream_error",
                        "message": str(exc),
                    }
                )

        return ToolResponse.success(
            {
                "dry_run": False,
                "deleted": deleted,
                "failed": failed,
                "delete_files": delete_files,
                "add_import_exclusion": add_import_exclusion,
                "summary": {
                    "requested": len(sonarr_ids),
                    "deleted": len(deleted),
                    "failed": len(failed),
                    "total_size_deleted_gb": sum_size_gb(deleted),
                },
            },
            warnings,
        )

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


async def sonarr_queue_action(
    services: Services,
    queue_id: int,
    action: str,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not queue_id:
            raise MediaMcpError("validation", "sonarr_queue_action requires queue_id.")
        if action not in SONARR_QUEUE_ACTIONS:
            raise MediaMcpError("validation", "Unsupported action.", {"allowed": sorted(SONARR_QUEUE_ACTIONS)})
        queue = await services.sonarr.get("/api/v3/queue", {"page": 1, "pageSize": 250})
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
        await services.sonarr.delete(
            f"/api/v3/queue/{queue_id}",
            bool_params({"removeFromClient": False, "blocklist": blocklist}),
        )
        return ToolResponse.success({"dry_run": False, "actioned": preview})

    return await safe_tool(run)


async def sonarr_calendar(services: Services, days_ahead: int = 14) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if days_ahead <= 0 or days_ahead > 90:
            raise MediaMcpError("validation", "days_ahead must be between 1 and 90.")
        start = datetime.now(UTC).date()
        end = start + timedelta(days=days_ahead)
        calendar = await services.sonarr.get(
            "/api/v3/calendar",
            {"start": start.isoformat(), "end": end.isoformat(), "includeSeries": "true"},
        )
        episodes = sorted(_as_list(calendar), key=lambda e: e.get("airDateUtc") or "")
        return ToolResponse.success(
            {
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "episodes": [
                    {
                        "sonarr_id": e.get("seriesId"),
                        "series_title": (e.get("series") or {}).get("title"),
                        "episode_title": e.get("title"),
                        "season_number": e.get("seasonNumber"),
                        "episode_number": e.get("episodeNumber"),
                        "air_date": e.get("airDate"),
                        "has_file": e.get("hasFile"),
                        "monitored": e.get("monitored"),
                    }
                    for e in episodes
                ],
            }
        )

    return await safe_tool(run)


async def sonarr_blocklist(services: Services, limit: int = 20) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit, default=20, maximum=100)
        blocklist = await services.sonarr.get(
            "/api/v3/blocklist",
            {"page": 1, "pageSize": bounded, "sortKey": "date", "sortDirection": "descending"},
        )
        records = _records(blocklist)
        total = blocklist.get("totalRecords") if isinstance(blocklist, dict) else len(records)
        return ToolResponse.success(
            {
                "total_records": total,
                "items": [
                    {
                        "blocklist_id": r.get("id"),
                        "sonarr_id": r.get("seriesId"),
                        "release_title": r.get("sourceTitle"),
                        "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                        "blocked_at": r.get("date"),
                        "reason": r.get("message"),
                    }
                    for r in records
                ],
            }
        )

    return await safe_tool(run)


async def sonarr_blocklist_remove(services: Services, blocklist_id: int, confirm: bool = False) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not blocklist_id:
            raise MediaMcpError("validation", "sonarr_blocklist_remove requires blocklist_id.")
        if not confirm:
            return ToolResponse.success({"dry_run": True, "would_remove": {"blocklist_id": blocklist_id}})
        await services.sonarr.delete(f"/api/v3/blocklist/{blocklist_id}")
        return ToolResponse.success({"dry_run": False, "removed": {"blocklist_id": blocklist_id}})

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
