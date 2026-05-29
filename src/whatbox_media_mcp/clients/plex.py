from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import requests
import urllib3
from plexapi.server import PlexServer

from whatbox_media_mcp.errors import UpstreamError


def iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


class PlexClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
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
        server = self._server()
        return [
            {
                "title": getattr(session, "title", None),
                "type": getattr(session, "type", None),
                "user": getattr(getattr(session, "user", None), "title", None),
                "state": getattr(getattr(session, "session", None), "state", None),
            }
            for session in server.sessions()  # type: ignore[no-untyped-call]
        ]

    async def recently_added(self, section_name: str, limit: int) -> list[dict[str, Any]]:
        section = self._section(section_name)
        return [self._summarize_item(item, section.title) for item in section.recentlyAdded(maxresults=limit)]

    async def search(self, section_name: str, query: str, limit: int) -> list[dict[str, Any]]:
        section = self._section(section_name)
        return [self._summarize_item(item, section.title) for item in section.search(title=query, limit=limit)]

    async def get_basic_library_items(self, section_name: str, limit: int) -> list[dict[str, Any]]:
        section = self._section(section_name)
        return [self._summarize_item(item, section.title) for item in section.search(limit=limit)]

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
            "file_paths": [cast(str, part.file) for part in parts if getattr(part, "file", None)],
        }

    @staticmethod
    def _duration_minutes(duration_ms: int | None) -> int | None:
        if duration_ms is None:
            return None
        return round(duration_ms / 60000)
