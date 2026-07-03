from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

# A pause is always time-bounded so filtering can't be left off by accident —
# AdGuard's own timer re-enables it. Default is short; the ceiling is an hour.
PAUSE_DEFAULT_MIN = 10
PAUSE_MAX_MIN = 60


def clamp_pause_minutes(minutes: int | None) -> int:
    """Pure. Bound a requested pause to [1, PAUSE_MAX_MIN], defaulting when
    unset. Keeps a mistyped or model-hallucinated duration from disabling
    network-wide filtering for an unreasonable window."""
    if minutes is None:
        return PAUSE_DEFAULT_MIN
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return PAUSE_DEFAULT_MIN
    return max(1, min(m, PAUSE_MAX_MIN))


async def adguard_protection(
    services: Services, action: str, minutes: int | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Pause or resume AdGuard network filtering (ad/tracker blocking) for the
    WHOLE LAN. action='pause' temporarily turns filtering off for a bounded
    number of minutes (default 10, max 60) and it AUTO-RE-ENABLES on AdGuard's
    own timer, so it can never be left off by accident; action='resume' turns
    it back on immediately. Two-step: confirm=false previews the current state
    and what would change; confirm=true applies it. Use for "turn off
    ad-blocking for a few minutes, a site is broken" and to turn it back on."""

    async def run() -> dict[str, Any]:
        if services.adguard is None:
            return ToolResponse.failure(
                "not_configured", "AdGuard is not configured (ADGUARD_PASSWORD unset)."
            )
        if action not in ("pause", "resume"):
            return ToolResponse.failure(
                "validation", "action must be 'pause' or 'resume'.", {"got": action}
            )
        current = await services.adguard.protection_state()
        currently_enabled = current.get("protection_enabled")

        if action == "pause":
            mins = clamp_pause_minutes(minutes)
            if not confirm:
                return ToolResponse.success(
                    {
                        "dry_run": True,
                        "action": "pause",
                        "minutes": mins,
                        "currently_enabled": currently_enabled,
                        "note": f"Would disable network-wide filtering for {mins} min, then AdGuard "
                        "auto-re-enables it. This affects every device on the LAN.",
                    }
                )
            await services.adguard.set_protection(False, mins * 60_000)
            after = await services.adguard.protection_state()
            return ToolResponse.success(
                {
                    "dry_run": False,
                    "action": "pause",
                    "auto_reenable_in_min": mins,
                    "protection_enabled_before": currently_enabled,
                    "protection_enabled_now": after.get("protection_enabled"),
                    "note": f"Filtering paused; it auto-re-enables in {mins} min. Call with "
                    "action=resume to turn it back on sooner.",
                }
            )

        # resume
        if not confirm:
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "action": "resume",
                    "currently_enabled": currently_enabled,
                    "note": "Would turn network-wide filtering back on immediately."
                    if currently_enabled is False
                    else "Filtering already appears on; resume is a no-op safety.",
                }
            )
        await services.adguard.set_protection(True)
        after = await services.adguard.protection_state()
        return ToolResponse.success(
            {
                "dry_run": False,
                "action": "resume",
                "protection_enabled_before": currently_enabled,
                "protection_enabled_now": after.get("protection_enabled"),
                "note": "Filtering re-enabled.",
            }
        )

    return await safe_tool(run)


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
