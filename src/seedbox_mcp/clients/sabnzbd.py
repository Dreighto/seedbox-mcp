from __future__ import annotations

from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError


class SabnzbdClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def queue(self) -> dict[str, Any]:
        return await self._cmd("queue")

    async def history(self, limit: int = 25) -> dict[str, Any]:
        return await self._cmd("history", limit=limit)

    async def _cmd(self, mode: str, **params: Any) -> dict[str, Any]:
        query = {"apikey": self.api_key, "mode": mode, "output": "json", **params}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api", params=query)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "SABnzbd is unreachable.",
                {"mode": mode, "reason": exc.__class__.__name__},
            ) from exc
        if response.is_error:
            raise UpstreamError(
                "upstream_unreachable",
                "SABnzbd returned an error.",
                {"mode": mode, "status_code": response.status_code},
            )
        return cast(dict[str, Any], response.json())
