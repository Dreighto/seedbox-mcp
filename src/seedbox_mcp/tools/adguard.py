from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


async def adguard_stats(services: Services) -> dict[str, Any]:
    """Network-wide ad/tracker blocking health from AdGuard Home: whether
    protection is on, total DNS queries, how many were blocked and the block
    rate, average processing time, and the top blocked domains + busiest
    client devices. Use to answer "how's the ad-blocking / network filtering
    doing" or "is AdGuard actually blocking". Read-only."""

    async def run() -> dict[str, Any]:
        if services.adguard is None:
            return ToolResponse.failure(
                "not_configured",
                "AdGuard is not configured (ADGUARD_PASSWORD unset).",
            )
        return ToolResponse.success(await services.adguard.stats_summary())

    return await safe_tool(run)
