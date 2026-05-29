from __future__ import annotations

import pytest

from tests.conftest import FakeArrClient
from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.tools.radarr import (
    radarr_add_movie,
    radarr_delete_movie,
    radarr_research_movie,
)
from whatbox_media_mcp.tools.search import media_search
from whatbox_media_mcp.tools.sonarr import (
    sonarr_add_series,
    sonarr_delete_series,
    sonarr_research_series,
)
from whatbox_media_mcp.tools.status import media_status


@pytest.mark.asyncio
async def test_media_status_returns_partial_success(settings) -> None:  # type: ignore[no-untyped-def]
    class FailingPlex:
        async def get_sections(self):  # type: ignore[no-untyped-def]
            from whatbox_media_mcp.errors import UpstreamError

            raise UpstreamError("upstream_unreachable", "Plex down.")

        async def get_sessions(self):  # type: ignore[no-untyped-def]
            return []

    radarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "5"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [],
        }
    )
    sonarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "4"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [],
        }
    )
    result = await media_status(Services(settings, radarr, sonarr, FailingPlex()))  # type: ignore[arg-type]
    assert result["ok"] is True
    assert result["data"]["plex"]["reachable"] is False
    assert result["warnings"]


@pytest.mark.asyncio
async def test_media_search_returns_existing_and_lookup_candidates(services: Services) -> None:
    result = await media_search(services, "Heat", limit=10)
    assert result["ok"] is True
    sources = {item["source"] for item in result["data"]["candidates"]}
    assert {"radarr", "radarr_lookup", "plex"} <= sources


@pytest.mark.asyncio
async def test_radarr_add_dry_run_does_not_post(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    result = await radarr_add_movie(services, tmdb_id=1538, confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.radarr.posts == []


@pytest.mark.asyncio
async def test_radarr_add_duplicate_returns_existing(services: Services) -> None:
    result = await radarr_add_movie(services, tmdb_id=949, confirm=True)
    assert result["data"]["action"] == "already_exists"
    assert services.radarr.posts == []


@pytest.mark.asyncio
async def test_radarr_delete_defaults_to_preserve_files(services: Services) -> None:
    result = await radarr_delete_movie(services, radarr_id=1, confirm=True)
    assert result["ok"] is True
    assert services.radarr.deletes[0][1] == {"deleteFiles": "false", "addImportExclusion": "false"}


@pytest.mark.asyncio
async def test_sonarr_add_dry_run_does_not_post(services: Services) -> None:
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    result = await sonarr_add_series(services, tvdb_id=79126, confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.sonarr.posts == []


@pytest.mark.asyncio
async def test_sonarr_delete_defaults_to_preserve_files(services: Services) -> None:
    result = await sonarr_delete_series(services, sonarr_id=2, confirm=True)
    assert result["ok"] is True
    assert services.sonarr.deletes[0][1] == {
        "deleteFiles": "false",
        "addImportListExclusion": "false",
    }


@pytest.mark.asyncio
async def test_research_tools_allow_only_known_commands(services: Services) -> None:
    radarr_result = await radarr_research_movie(services, radarr_id=1, mode="unsupported")
    sonarr_result = await sonarr_research_series(services, sonarr_id=2, mode="unsupported")
    assert radarr_result["ok"] is False
    assert sonarr_result["ok"] is False
    assert radarr_result["error_type"] == "validation"
    assert sonarr_result["error_type"] == "validation"
