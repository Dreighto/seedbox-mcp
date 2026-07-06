from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from seedbox_mcp.config import Settings
from seedbox_mcp.runtime import Services


class FakeArrClient:
    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.puts: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[tuple[str, dict[str, Any] | None]] = []
        # Mapping of path -> Exception. When set, delete(path, ...) records the
        # call (so callers can assert it was attempted) and then raises.
        self.delete_errors: dict[str, Exception] = {}

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

    async def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.puts.append((path, payload))
        result = self.routes.get(("PUT", path))
        if result is not None:
            return result
        return payload

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.deletes.append((path, params))
        if path in self.delete_errors:
            raise self.delete_errors[path]
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

    async def search(
        self,
        section_name: str,
        query: str | None = None,
        limit: int = 10,
        plex_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "movie",
                "title": query or "",
                "year": 1995,
                "section": section_name,
                "rating_key": "1",
                "directors": ["Michael Mann"],
            }
        ][:limit]

    async def get_basic_library_items(self, section_name: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
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
        ][offset : offset + limit]


@pytest.fixture
def settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    # Hermetic: do NOT load the developer's real .env (it carries deployment
    # overrides like PLEX_VERIFY_TLS=false that would pollute default-value tests).
    return Settings(
        _env_file=None,
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
        oauth_state_path=tmp_path / "oauth_state.json",
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
            ("GET", "/api/v3/queue"): {
                "records": [
                    {
                        "id": 10,
                        "title": "Heat.1995.1080p.BluRay.x264-GROUP",
                        "movieId": 1,
                        "movie": {"id": 1, "title": "Heat"},
                        "status": "completed",
                        "trackedDownloadState": "importBlocked",
                    },
                ]
            },
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
                    "seasons": [
                        {
                            "seasonNumber": 1,
                            "monitored": True,
                            "statistics": {"episodeFileCount": 13, "episodeCount": 13, "sizeOnDisk": 10 * 1024**3},
                        },
                        {
                            "seasonNumber": 2,
                            "monitored": False,
                            "statistics": {"episodeFileCount": 0, "episodeCount": 12, "sizeOnDisk": 0},
                        },
                    ],
                }
            ],
            ("GET", "/api/v3/series/lookup"): [{"title": "The Wire", "year": 2002, "tvdbId": 79126}],
            ("GET", "/api/v3/series/2"): {
                "id": 2,
                "title": "The Wire",
                "path": "/media/TV/The Wire",
                "monitored": True,
                "seasons": [
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": False},
                ],
            },
            ("GET", "/api/v3/queue"): {
                "records": [
                    {
                        "id": 20,
                        "title": "The.Wire.S01E01.1080p.BluRay.x264-GROUP",
                        "seriesId": 2,
                        "series": {"id": 2, "title": "The Wire"},
                        "status": "completed",
                        "trackedDownloadState": "importBlocked",
                    },
                ]
            },
            ("GET", "/api/v3/wanted/missing"): {"records": []},
        }
    )
    return Services(settings, radarr, sonarr, FakePlexClient())  # type: ignore[arg-type]
