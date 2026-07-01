from __future__ import annotations

from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError


class NasdoomClient:
    """Thin client for the NASDOOM BFF (~/dev/nasdoom, docs/api-v1.md).

    Tailnet-private, no auth at the BFF edge — it injects every upstream
    credential server-side. Prefer this over the direct Radarr/Sonarr/
    Prowlarr/SABnzbd/Jellyseerr clients for anything NASDOOM already
    consolidates (queue, requests, storage-with-denominator, cross-source
    search) — it does the reconciliation work once instead of every caller
    re-deriving it, and keeps the bot's view consistent with the app's.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("POST", path, json_body=json_body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_path = "/" + path.lstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.request(
                    method, f"{self.base_url}{clean_path}", params=params, json=json_body
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "NASDOOM BFF is unreachable.",
                {"path": clean_path, "reason": exc.__class__.__name__},
            ) from exc
        if response.is_error:
            # 4xx from the BFF is often informative (e.g. 422
            # unsupported_on_import_lane, 409 already_managed) — surface the
            # body instead of collapsing everything to "unreachable", so the
            # model can explain *why* an action was rejected.
            detail: Any = None
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise UpstreamError(
                "validation" if response.status_code < 500 else "upstream_unreachable",
                "NASDOOM BFF rejected the request." if response.status_code < 500 else "NASDOOM BFF returned an error.",
                {"path": clean_path, "status_code": response.status_code, "body": detail},
            )
        return cast(dict[str, Any], response.json())
