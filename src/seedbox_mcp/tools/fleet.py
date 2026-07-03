from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


def _group(monitors: list[dict[str, Any]]) -> dict[str, Any]:
    down = [m["name"] for m in monitors if m["status"] == 0]
    not_checked = [m["name"] for m in monitors if m["status"] in (2, 3)]
    return {
        "total": len(monitors),
        "up": sum(1 for m in monitors if m["up"]),
        "down": down,
        "not_checked": not_checked,
    }


async def fleet_health(services: Services) -> dict[str, Any]:
    """Whole-cluster health from Uptime Kuma: every node AND service in one
    call. This is a superset of nasdoom_health (which is media services only)
    — it also covers the cluster nodes (ROOM, apple-node, Jetson, ...) and the
    non-media services (AdGuard, Vaultwarden, monitoring). Use it to answer
    "is everything up across the NAS/cluster" and to catch a node or
    non-media outage the media-only checks would miss."""

    async def run() -> dict[str, Any]:
        if services.uptime_kuma is None:
            return ToolResponse.failure(
                "not_configured",
                "Fleet monitoring is not configured (UPTIME_KUMA_API_KEY unset).",
            )
        monitors = await services.uptime_kuma.fleet_status()
        if not monitors:
            return ToolResponse.failure(
                "no_data", "Uptime Kuma returned no monitors."
            )
        nodes = [m for m in monitors if m["name"].startswith("Node:")]
        svcs = [m for m in monitors if not m["name"].startswith("Node:")]
        down_all = [m["name"] for m in monitors if m["status"] == 0]
        return ToolResponse.success(
            {
                "all_ok": not down_all,
                "down": down_all,
                "checked": len(monitors),
                "nodes": _group(nodes),
                "services": _group(svcs),
            }
        )

    return await safe_tool(run)
