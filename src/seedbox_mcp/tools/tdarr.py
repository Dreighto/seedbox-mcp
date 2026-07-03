from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


async def tdarr_status(services: Services) -> dict[str, Any]:
    """HEVC transcoding pipeline status from Tdarr: each node (the NAS
    internal node and the ROOM RTX 5060 Ti GPU node) with paused/worker
    state, whether anything is actively processing, lifetime transcode +
    health-check counts, and space reclaimed (only counts files Tdarr itself
    processed — manual/out-of-band re-encodes don't show here). Use for
    "how's the transcoding", "is the GPU node up", "how much space has Tdarr
    saved". Read-only."""

    async def run() -> dict[str, Any]:
        if services.tdarr is None:
            return ToolResponse.failure(
                "not_configured", "Tdarr is not configured (TDARR_URL unset)."
            )
        return ToolResponse.success(await services.tdarr.status())

    return await safe_tool(run)
