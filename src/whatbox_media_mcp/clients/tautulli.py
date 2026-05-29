from __future__ import annotations

from typing import Any, cast

import httpx

from whatbox_media_mcp.errors import UpstreamError


class TautulliClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def get_activity(self) -> dict[str, Any]:
        return await self._cmd("get_activity")

    async def get_history(self, limit: int) -> dict[str, Any]:
        return await self._cmd("get_history", length=limit)

    async def get_recently_added(self, limit: int) -> dict[str, Any]:
        return await self._cmd("get_recently_added", count=limit)

    async def get_user_stats(self) -> dict[str, Any]:
        return await self._cmd("get_user_watch_time_stats")

    async def _cmd(self, cmd: str, **params: Any) -> dict[str, Any]:
        query = {"apikey": self.api_key, "cmd": cmd, **params}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/v2", params=query)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Tautulli is unreachable.",
                {"cmd": cmd, "reason": exc.__class__.__name__},
            ) from exc
        if response.is_error:
            raise UpstreamError(
                "upstream_unreachable",
                "Tautulli returned an error.",
                {"cmd": cmd, "status_code": response.status_code},
            )
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload.get("response", {}).get("data", payload))
