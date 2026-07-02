from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import (
    clamp_limit,
    compact_plex_item,
    compact_tautulli_activity,
    partial_call,
    safe_tool,
)


async def plex_now_playing(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        # Source of truth is Plex's own /status/sessions, NOT Tautulli's
        # get_activity — found live 2026-07-02 that Tautulli was
        # DISCONNECTED from Plex (stale 127.0.0.1/Windows config left over
        # from the NAS's move to Docker), so its activity returned an empty
        # {} even with a real stream playing while Plex reported it
        # correctly. Plex is authoritative for live streams.
        sessions = await services.plex.get_sessions()
        transcoding = [s for s in sessions if s.get("is_transcoding")]
        remote = [s for s in sessions if s.get("local") is False]
        total_bw = sum(s.get("bandwidth_kbps") or 0 for s in sessions)
        # Bottleneck flags computed in code, not left to model judgment:
        # transcodes are CPU/GPU load, remote streams are WAN-upload load,
        # a throttled transcode means the server can't keep up in realtime.
        throttled = [s for s in transcoding if (s.get("transcode") or {}).get("throttled")]
        flags = []
        if len(transcoding) >= 2:
            flags.append(f"{len(transcoding)} concurrent transcodes — CPU/GPU load")
        if throttled:
            flags.append(f"{len(throttled)} transcode(s) throttled — server may not be keeping up in real time")
        if remote:
            flags.append(f"{len(remote)} remote (WAN) stream(s) — upload-bandwidth load")
        if total_bw >= 100_000:
            flags.append(f"total stream bandwidth ~{round(total_bw / 1000)} Mbps")
        return ToolResponse.success(
            {
                "stream_count": len(sessions),
                "transcode_count": len(transcoding),
                "remote_count": len(remote),
                "total_bandwidth_mbps": round(total_bw / 1000, 1) if total_bw else 0,
                "bottleneck_flags": flags,
                "now_playing": [
                    {
                        "user": s.get("user"),
                        "title": s.get("show") or s.get("title"),
                        "detail": s.get("title") if s.get("show") else None,
                        "type": s.get("type"),
                        "state": s.get("state"),
                        "progress_percent": s.get("progress_pct"),
                        "player": s.get("player"),
                        "where": "remote (WAN)" if s.get("local") is False else "local (LAN)",
                        "playback": "transcode" if s.get("is_transcoding") else (s.get("stream_decision") or "direct"),
                        "bandwidth_mbps": round((s.get("bandwidth_kbps") or 0) / 1000, 1),
                        "source_resolution": s.get("source_resolution"),
                        "transcode": s.get("transcode"),
                    }
                    for s in sessions
                ],
            }
        )

    return await safe_tool(run)


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
                recently_added.extend(compact_plex_item(item) for item in items or [])
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
                    compact_plex_item(item)
                    for item in items or []
                    if not item.get("last_viewed_at") and not item.get("view_count")
                )
            data["basic_staleness_candidates"] = stale[:bounded]

        if services.tautulli:
            tautulli_activity, warning = await partial_call(services.tautulli.get_activity)
            if warning:
                warnings.append(f"tautulli activity: {warning}")
            else:
                data["tautulli_activity"] = compact_tautulli_activity(tautulli_activity or {})

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
