from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


async def gotify_alerts(services: Services, limit: int = 10) -> dict[str, Any]:
    """Recent alert history from the Gotify inbox — what has actually FIRED
    lately (Uptime Kuma up/down events for nodes and services, and anything
    else pushed there). This is the temporal view that fleet_health (a
    point-in-time snapshot) doesn't give: use it for "what alerts fired
    recently", "what went wrong today", "has anything been flapping". `limit`
    is how many most-recent alerts to return. Read-only."""

    async def run() -> dict[str, Any]:
        if services.gotify is None:
            return ToolResponse.failure(
                "not_configured",
                "Alert history is not configured (GOTIFY_CLIENT_TOKEN unset).",
            )
        alerts = await services.gotify.recent(limit)
        return ToolResponse.success({"count": len(alerts), "alerts": alerts})

    return await safe_tool(run)
