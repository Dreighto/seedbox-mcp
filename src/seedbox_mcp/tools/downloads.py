from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import clamp_limit, partial_call, safe_tool


async def prowlarr_overview(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        prowlarr = services.prowlarr
        if not prowlarr:
            return ToolResponse.failure("prowlarr_unavailable", "Prowlarr is not configured.")
        warnings: list[str] = []
        status, warning = await partial_call(lambda: prowlarr.get("/api/v1/system/status"))
        if warning:
            warnings.append(f"status: {warning}")
        health, warning = await partial_call(lambda: prowlarr.get("/api/v1/health"))
        if warning:
            warnings.append(f"health: {warning}")
        indexers, warning = await partial_call(lambda: prowlarr.get("/api/v1/indexer"))
        if warning:
            warnings.append(f"indexers: {warning}")
        indexer_list = indexers if isinstance(indexers, list) else []
        return ToolResponse.success(
            {
                "reachable": status is not None,
                "version": (status or {}).get("version") if isinstance(status, dict) else None,
                "health": health or [],
                "indexer_count": len(indexer_list),
                "indexers_enabled": sum(1 for i in indexer_list if i.get("enable")),
                "indexers_disabled": [
                    i.get("name") for i in indexer_list if not i.get("enable")
                ],
            },
            warnings,
        )

    return await safe_tool(run)


async def sabnzbd_overview(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        sabnzbd = services.sabnzbd
        if not sabnzbd:
            return ToolResponse.failure("sabnzbd_unavailable", "SABnzbd is not configured.")
        warnings: list[str] = []
        queue_raw, warning = await partial_call(sabnzbd.queue)
        if warning:
            warnings.append(f"queue: {warning}")
        history_raw, warning = await partial_call(lambda: sabnzbd.history(limit=25))
        if warning:
            warnings.append(f"history: {warning}")

        queue = (queue_raw or {}).get("queue", {})
        slots = queue.get("slots", []) if isinstance(queue, dict) else []
        history = (history_raw or {}).get("history", {})
        history_slots = history.get("slots", []) if isinstance(history, dict) else []
        recent_failed = [
            {"name": s.get("name"), "fail_message": s.get("fail_message")}
            for s in history_slots
            if isinstance(s, dict) and s.get("status") == "Failed"
        ][:10]

        return ToolResponse.success(
            {
                "reachable": queue_raw is not None,
                "paused": queue.get("paused"),
                "speed": queue.get("speed"),
                "size_left": queue.get("sizeleft"),
                "eta": queue.get("timeleft"),
                "queue_items": len(slots),
                "queue_preview": [
                    {"name": s.get("filename"), "status": s.get("status"), "percentage": s.get("percentage")}
                    for s in slots[:10]
                    if isinstance(s, dict)
                ],
                "recent_failed_downloads": recent_failed,
            },
            warnings,
        )

    return await safe_tool(run)


async def jellyseerr_overview(services: Services, limit: int = 20) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        jellyseerr = services.jellyseerr
        if not jellyseerr:
            return ToolResponse.failure("jellyseerr_unavailable", "Jellyseerr is not configured.")
        bounded = clamp_limit(limit, default=20, maximum=100)
        warnings: list[str] = []
        counts, warning = await partial_call(lambda: jellyseerr.get("/api/v1/request/count"))
        if warning:
            warnings.append(f"counts: {warning}")
        pending, warning = await partial_call(
            lambda: jellyseerr.get("/api/v1/request", {"filter": "pending", "take": bounded, "sort": "added"})
        )
        if warning:
            warnings.append(f"pending: {warning}")
        pending_results = (pending or {}).get("results", []) if isinstance(pending, dict) else []
        return ToolResponse.success(
            {
                "reachable": counts is not None,
                "counts": counts or {},
                "pending_requests": [_compact_request(r) for r in pending_results],
            },
            warnings,
        )

    return await safe_tool(run)


def _compact_request(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media") or {}
    requested_by = item.get("requestedBy") or {}
    return {
        "request_id": item.get("id"),
        "media_type": item.get("type"),
        "status": item.get("status"),
        "tmdb_id": media.get("tmdbId"),
        "requested_by": requested_by.get("displayName") or requested_by.get("username"),
        "created_at": item.get("createdAt"),
    }
