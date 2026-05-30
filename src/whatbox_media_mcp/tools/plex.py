from __future__ import annotations

from typing import Any

from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import clamp_limit, partial_call, safe_tool


async def plex_overview(
    services: Services,
    section: str = "all",
    include_activity: bool = True,
    include_recently_added: bool = True,
    include_staleness: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        bounded = clamp_limit(limit)
        warnings: list[str] = []
        data: dict[str, Any] = {"limit": bounded}
        sections = _requested_sections(services, section)
        data["sections"] = sections

        if include_activity:
            sessions, warning = await partial_call(services.plex.get_sessions)
            if warning:
                warnings.append(f"plex activity: {warning}")
                sessions = []
            data["active_sessions"] = sessions

        if include_recently_added:
            recently_added: list[dict[str, Any]] = []
            for name in sections:

                async def get_recent(name: str = name) -> list[dict[str, Any]]:
                    return await services.plex.recently_added(name, bounded)

                items, warning = await partial_call(get_recent)
                if warning:
                    warnings.append(f"plex recently added {name}: {warning}")
                    continue
                recently_added.extend(items or [])
            data["recently_added"] = recently_added[:bounded]

        if include_staleness:
            stale: list[dict[str, Any]] = []
            for name in sections:

                async def get_items(name: str = name) -> list[dict[str, Any]]:
                    return await services.plex.get_basic_library_items(name, bounded)

                items, warning = await partial_call(get_items)
                if warning:
                    warnings.append(f"plex staleness {name}: {warning}")
                    continue
                stale.extend(
                    item for item in items or [] if not item.get("last_viewed_at") and not item.get("view_count")
                )
            data["basic_staleness_candidates"] = stale[:bounded]

        if services.tautulli:
            tautulli_activity, warning = await partial_call(services.tautulli.get_activity)
            if warning:
                warnings.append(f"tautulli activity: {warning}")
            else:
                data["tautulli_activity"] = tautulli_activity

        return ToolResponse.success(data, warnings)

    return await safe_tool(run)


async def plex_library_size(
    services: Services,
    section: str = "all",
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        warnings: list[str] = []
        sections = _requested_sections(services, section)
        results = []
        for name in sections:

            async def get_size(name: str = name) -> dict[str, Any]:
                return await services.plex.get_library_size(name)

            size, warning = await partial_call(get_size)
            if warning:
                warnings.append(f"plex library size {name}: {warning}")
            elif size:
                results.append(size)
        combined = round(sum(r["total_gb"] for r in results), 2)
        return ToolResponse.success({"sections": results, "combined_total_gb": combined}, warnings)

    return await safe_tool(run)


def _requested_sections(services: Services, section: str) -> list[str]:
    if section == "movies":
        return [services.settings.plex_movie_section]
    if section == "tv":
        return [services.settings.plex_tv_section]
    return [services.settings.plex_movie_section, services.settings.plex_tv_section]
