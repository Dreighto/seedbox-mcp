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

    async def get_history(
        self,
        limit: int = 100,
        user: str | None = None,
        rating_key: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"length": limit}
        if user:
            params["user"] = user
        if rating_key:
            params["rating_key"] = rating_key
        if start_date:
            params["after"] = start_date
        if end_date:
            params["before"] = end_date
        if media_type:
            params["media_type"] = media_type
        return await self._cmd("get_history", **params)

    async def get_recently_added(self, limit: int) -> dict[str, Any]:
        return await self._cmd("get_recently_added", count=limit)

    async def get_users(self) -> dict[str, Any]:
        return await self._cmd("get_users")

    async def get_user_stats(self, user_id: int | None = None, grouping: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {"grouping": grouping}
        if user_id is not None:
            params["user_id"] = user_id
        return await self._cmd("get_user_watch_time_stats", **params)

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
