from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from whatbox_media_mcp.config import Settings
from whatbox_media_mcp.runtime import Services


class FakeArrClient:
    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = ("GET", path)
        if key in self.routes:
            return self.routes[key]
        raise AssertionError(f"Unexpected GET {path} {params}")

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, payload))
        result = self.routes.get(("POST", path))
        if result is not None:
            return result
        return {"id": 999, **payload}

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.deletes.append((path, params))
        return self.routes.get(("DELETE", path))


class FakePlexClient:
    async def get_sections(self) -> list[str]:
        return ["Movies", "TV Shows"]

    async def get_sessions(self) -> list[dict[str, Any]]:
        return [{"title": "Heat", "type": "movie", "state": "playing"}]

    async def recently_added(self, section_name: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "type": "movie",
                "title": f"Recent {section_name}",
                "section": section_name,
                "rating_key": "1",
                "view_count": 0,
            }
        ][:limit]

    async def search(self, section_name: str, query: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "type": "movie",
                "title": query,
                "year": 1995,
                "section": section_name,
                "rating_key": "1",
            }
        ][:limit]

    async def get_basic_library_items(self, section_name: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "type": "movie",
                "title": "Heat",
                "section": section_name,
                "rating_key": "1",
                "added_at": "2026-01-01T00:00:00+00:00",
                "last_viewed_at": None,
                "view_count": 0,
            }
        ][:limit]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        mcp_bearer_token=SecretStr("dev"),
        radarr_url="http://radarr.local",
        radarr_api_key=SecretStr("radarr-key"),
        radarr_default_root_folder="/media/Movies",
        radarr_default_quality_profile_id=1,
        sonarr_url="http://sonarr.local",
        sonarr_api_key=SecretStr("sonarr-key"),
        sonarr_default_root_folder="/media/TV",
        sonarr_default_quality_profile_id=1,
        plex_url="http://plex.local",
        plex_token=SecretStr("plex-token"),
    )


@pytest.fixture
def services(settings: Settings) -> Services:
    radarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "5.0.0"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [{"path": "/media", "freeSpace": 1073741824}],
            ("GET", "/api/v3/movie"): [
                {
                    "id": 1,
                    "title": "Heat",
                    "year": 1995,
                    "tmdbId": 949,
                    "imdbId": "tt0113277",
                    "hasFile": True,
                    "monitored": True,
                    "path": "/media/Movies/Heat",
                }
            ],
            ("GET", "/api/v3/movie/lookup"): [{"title": "Heat", "year": 1995, "tmdbId": 949, "imdbId": "tt0113277"}],
            ("GET", "/api/v3/movie/lookup/tmdb"): {
                "title": "Collateral",
                "year": 2004,
                "tmdbId": 1538,
            },
            ("GET", "/api/v3/movie/1"): {
                "id": 1,
                "title": "Heat",
                "year": 1995,
                "path": "/media/Movies/Heat",
                "monitored": True,
            },
            ("GET", "/api/v3/queue"): {"records": []},
            ("GET", "/api/v3/wanted/missing"): {"records": []},
        }
    )
    sonarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "4.0.0"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [{"path": "/media", "freeSpace": 1073741824}],
            ("GET", "/api/v3/series"): [
                {
                    "id": 2,
                    "title": "The Wire",
                    "year": 2002,
                    "tvdbId": 79126,
                    "monitored": True,
                    "path": "/media/TV/The Wire",
                    "statistics": {"episodeFileCount": 60, "episodeCount": 60},
                }
            ],
            ("GET", "/api/v3/series/lookup"): [{"title": "The Wire", "year": 2002, "tvdbId": 79126}],
            ("GET", "/api/v3/series/2"): {
                "id": 2,
                "title": "The Wire",
                "path": "/media/TV/The Wire",
                "monitored": True,
            },
            ("GET", "/api/v3/queue"): {"records": []},
            ("GET", "/api/v3/wanted/missing"): {"records": []},
        }
    )
    return Services(settings, radarr, sonarr, FakePlexClient())  # type: ignore[arg-type]
