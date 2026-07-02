from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import requests
import urllib3
from plexapi.server import PlexServer

from seedbox_mcp.errors import UpstreamError


def iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


class PlexClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.verify = self.verify_tls
        if not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return session

    def _server(self) -> PlexServer:
        try:
            return PlexServer(self.base_url, self.token, session=self._session())  # type: ignore[no-untyped-call]
        except Exception as exc:  # plexapi raises several connection/auth exceptions.
            raise UpstreamError(
                "upstream_unreachable",
                "Plex is unreachable or rejected credentials.",
                {"reason": exc.__class__.__name__, "detail": str(exc)},
            ) from exc

    async def get_sections(self) -> list[str]:
        server = self._server()
        return [section.title for section in server.library.sections()]

    async def get_sessions(self) -> list[dict[str, Any]]:
        # Read /status/sessions raw rather than via plexapi's session
        # objects — the raw payload carries the bottleneck-relevant fields
        # (per-stream bandwidth, lan/wan location, transcode decision and
        # throttle/speed, source vs streamed resolution) that plexapi
        # flattens away. This is the authoritative live-stream source;
        # Tautulli's get_activity is unreliable (found disconnected from
        # Plex after the NAS's Windows->Docker migration, 2026-07-02).
        session = self._session()
        try:
            resp = session.get(
                f"{self.base_url}/status/sessions",
                headers={"X-Plex-Token": self.token, "Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Plex is unreachable or rejected credentials.",
                {"reason": exc.__class__.__name__, "detail": str(exc)},
            ) from exc
        results = []
        for m in payload.get("MediaContainer", {}).get("Metadata", []):
            player = m.get("Player") or {}
            sess = m.get("Session") or {}
            transcode = m.get("TranscodeSession")
            media = (m.get("Media") or [{}])[0]
            part = (media.get("Part") or [{}])[0]
            part_decision = part.get("decision")  # directplay | copy | transcode
            duration = m.get("duration")
            offset = m.get("viewOffset")
            progress_pct = round(offset / duration * 100, 1) if duration and offset is not None else None
            results.append(
                {
                    "title": m.get("title"),
                    "show": m.get("grandparentTitle"),  # show name for an episode; None for a movie
                    "type": m.get("type"),
                    "user": (m.get("User") or {}).get("title"),
                    "state": player.get("state"),
                    "progress_pct": progress_pct,
                    "player": player.get("title"),
                    # Bottleneck-relevant, per the operator's monitoring need:
                    "local": player.get("local"),  # True = inside the network, False = remote
                    "location": sess.get("location"),  # 'lan' | 'wan'
                    "bandwidth_kbps": sess.get("bandwidth"),
                    "source_resolution": media.get("videoResolution"),
                    "source_bitrate_kbps": media.get("bitrate"),
                    "stream_decision": part_decision,
                    "is_transcoding": bool(transcode) or part_decision == "transcode",
                    "transcode": (
                        {
                            "video_decision": transcode.get("videoDecision"),
                            "audio_decision": transcode.get("audioDecision"),
                            "throttled": transcode.get("throttled"),
                            "speed": transcode.get("speed"),
                            "progress": transcode.get("progress"),
                        }
                        if transcode
                        else None
                    ),
                }
            )
        return results

    async def recently_added(self, section_name: str, limit: int) -> list[dict[str, Any]]:
        section = self._section(section_name)
        return [self._summarize_item(item, section.title) for item in section.recentlyAdded(maxresults=limit)]

    async def search(
        self,
        section_name: str,
        query: str | None = None,
        limit: int = 10,
        plex_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        section = self._section(section_name)
        search_kwargs: dict[str, Any] = {"limit": limit}
        if query:
            search_kwargs["title"] = query
        if plex_filters:
            search_kwargs["filters"] = plex_filters
        return [self._summarize_item(item, section.title) for item in section.search(**search_kwargs)]

    async def get_basic_library_items(self, section_name: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        section = self._section(section_name)
        items = section.search(maxresults=limit, container_start=offset, container_size=limit)
        return [self._summarize_item(item, section.title) for item in items]

    async def get_library_size(self, section_name: str) -> dict[str, Any]:
        section = self._section(section_name)
        total_bytes = 0
        # For TV sections, size lives on episodes not shows — use searchEpisodes().
        # For movie sections, all() returns Movie objects which have media directly.
        if section.type == "show":
            items = section.searchEpisodes()  # type: ignore[attr-defined]
            item_count = len({getattr(ep, "grandparentRatingKey", None) for ep in items})
        else:
            items = section.all()
            item_count = len(items)
        for item in items:
            for medium in getattr(item, "media", []) or []:
                for part in getattr(medium, "parts", []) or []:
                    total_bytes += getattr(part, "size", None) or 0
        return {
            "section": section.title,
            "total_gb": round(total_bytes / 1024**3, 2),
            "item_count": item_count,
        }

    def _section(self, section_name: str) -> Any:
        server = self._server()
        try:
            return server.library.section(section_name)
        except Exception as exc:
            raise UpstreamError(
                "not_found",
                "Plex library section was not found.",
                {"section": section_name},
            ) from exc

    def _summarize_item(self, item: Any, section: str) -> dict[str, Any]:
        media = getattr(item, "media", []) or []
        parts: list[Any] = []
        for medium in media:
            parts.extend(getattr(medium, "parts", []) or [])
        total_size = sum(getattr(p, "size", None) or 0 for p in parts) or None
        directors = [d.tag for d in getattr(item, "directors", []) or []] or None
        return {
            "type": getattr(item, "type", None),
            "title": getattr(item, "title", None),
            "year": getattr(item, "year", None),
            "section": section,
            "rating_key": str(getattr(item, "ratingKey", "")),
            "added_at": iso_datetime(getattr(item, "addedAt", None)),
            "last_viewed_at": iso_datetime(getattr(item, "lastViewedAt", None)),
            "view_count": getattr(item, "viewCount", None),
            "duration_minutes": self._duration_minutes(getattr(item, "duration", None)),
            "size_on_disk_gb": round(total_size / 1024**3, 2) if total_size else None,
            "file_paths": [cast(str, part.file) for part in parts if getattr(part, "file", None)],
            "directors": directors,
        }

    @staticmethod
    def _duration_minutes(duration_ms: int | None) -> int | None:
        if duration_ms is None:
            return None
        return round(duration_ms / 60000)
