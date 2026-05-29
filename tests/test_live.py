from __future__ import annotations

import os

import pytest

from whatbox_media_mcp.config import load_settings
from whatbox_media_mcp.runtime import Services, build_services
from whatbox_media_mcp.tools.radarr import radarr_overview
from whatbox_media_mcp.tools.sonarr import sonarr_overview
from whatbox_media_mcp.tools.status import media_status

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_TESTS"), reason="set LIVE_TESTS=1 to run live integration tests"),
]


@pytest.fixture(scope="module")
def live_services() -> Services:
    return build_services(load_settings())


@pytest.mark.asyncio
async def test_live_all_services_reachable(live_services: Services) -> None:
    result = await media_status(live_services)
    assert result["ok"] is True
    data = result["data"]
    assert data["radarr"]["reachable"], f"Radarr unreachable: {result.get('warnings')}"
    assert data["sonarr"]["reachable"], f"Sonarr unreachable: {result.get('warnings')}"
    assert data["plex"]["reachable"], f"Plex unreachable — warnings: {result.get('warnings')} — full result: {result}"
    assert data["radarr"]["version"], "Radarr version missing — API key may be wrong"
    assert data["sonarr"]["version"], "Sonarr version missing — API key may be wrong"


@pytest.mark.asyncio
async def test_live_radarr_returns_library(live_services: Services) -> None:
    result = await radarr_overview(live_services, include_movies=True, include_queue=False, include_missing=False)
    assert result["ok"] is True
    assert isinstance(result["data"].get("movies"), list)


@pytest.mark.asyncio
async def test_live_sonarr_returns_library(live_services: Services) -> None:
    result = await sonarr_overview(live_services, include_series=True, include_queue=False, include_missing=False)
    assert result["ok"] is True
    assert isinstance(result["data"].get("series"), list)
