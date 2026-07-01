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
        clean_path = "/" + path.lstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{self.base_url}{clean_path}", params=params)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "NASDOOM BFF is unreachable.",
                {"path": clean_path, "reason": exc.__class__.__name__},
            ) from exc
        if response.is_error:
            raise UpstreamError(
                "upstream_unreachable",
                "NASDOOM BFF returned an error.",
                {"path": clean_path, "status_code": response.status_code},
            )
        return cast(dict[str, Any], response.json())
