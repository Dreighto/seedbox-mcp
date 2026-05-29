from __future__ import annotations

import httpx
import pytest
import respx

from whatbox_media_mcp.clients.arr import ArrClient
from whatbox_media_mcp.errors import UpstreamError


@respx.mock
@pytest.mark.asyncio
async def test_arr_client_sends_api_key() -> None:
    route = respx.get("http://radarr.local/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"})
    )
    client = ArrClient("http://radarr.local", "secret")
    assert await client.get("/api/v3/system/status") == {"version": "5"}
    assert route.calls[0].request.headers["X-Api-Key"] == "secret"


@respx.mock
@pytest.mark.asyncio
async def test_arr_client_maps_401() -> None:
    respx.get("http://radarr.local/api/v3/system/status").mock(return_value=httpx.Response(401, json={}))
    client = ArrClient("http://radarr.local", "bad")
    with pytest.raises(UpstreamError) as exc:
        await client.get("/api/v3/system/status")
    assert exc.value.error_type == "upstream_auth"
