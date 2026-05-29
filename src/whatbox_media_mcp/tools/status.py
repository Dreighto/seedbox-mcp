from __future__ import annotations

from typing import Any

from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import partial_call, safe_tool


async def media_status(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        warnings: list[str] = []
        radarr_status, warning = await partial_call(lambda: services.radarr.get("/api/v3/system/status"))
        if warning:
            warnings.append(f"radarr: {warning}")
        radarr_health, warning = await partial_call(lambda: services.radarr.get("/api/v3/health"))
        if warning:
            warnings.append(f"radarr health: {warning}")
        radarr_disk, warning = await partial_call(lambda: services.radarr.get("/api/v3/diskspace"))
        if warning:
            warnings.append(f"radarr disk: {warning}")

        sonarr_status, warning = await partial_call(lambda: services.sonarr.get("/api/v3/system/status"))
        if warning:
            warnings.append(f"sonarr: {warning}")
        sonarr_health, warning = await partial_call(lambda: services.sonarr.get("/api/v3/health"))
        if warning:
            warnings.append(f"sonarr health: {warning}")
        sonarr_disk, warning = await partial_call(lambda: services.sonarr.get("/api/v3/diskspace"))
        if warning:
            warnings.append(f"sonarr disk: {warning}")

        plex_sections, warning = await partial_call(services.plex.get_sections)
        if warning:
            warnings.append(f"plex sections: {warning}")
        plex_sessions, warning = await partial_call(services.plex.get_sessions)
        if warning:
            warnings.append(f"plex sessions: {warning}")

        return ToolResponse.success(
            {
                "radarr": {
                    "reachable": radarr_status is not None,
                    "version": (radarr_status or {}).get("version") if isinstance(radarr_status, dict) else None,
                    "health": radarr_health or [],
                    "disk": _disk_summary(radarr_disk),
                },
                "sonarr": {
                    "reachable": sonarr_status is not None,
                    "version": (sonarr_status or {}).get("version") if isinstance(sonarr_status, dict) else None,
                    "health": sonarr_health or [],
                    "disk": _disk_summary(sonarr_disk),
                },
                "plex": {
                    "reachable": plex_sections is not None,
                    "active_sessions": len(plex_sessions or []),
                    "sections": plex_sections or [],
                },
            },
            warnings,
        )

    return await safe_tool(run)


def _disk_summary(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        {
            "path": item.get("path"),
            "free_gb": round(item.get("freeSpace", 0) / 1024**3, 1),
        }
        for item in items
        if isinstance(item, dict)
    ]
