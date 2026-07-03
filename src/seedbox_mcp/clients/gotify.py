from __future__ import annotations

from typing import Any

import httpx

from seedbox_mcp.errors import UpstreamError


class GotifyClient:
    """Reads the Gotify alert inbox — the history of what has fired (Uptime
    Kuma up/down events and anything else pushed there). Auth is the client
    token via the X-Gotify-Key header."""

    def __init__(self, url: str, client_token: str) -> None:
        self.url = url.rstrip("/")
        self.token = client_token

    async def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        n = max(1, min(limit, 100))
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self.url}/message",
                    params={"limit": n},
                    headers={"X-Gotify-Key": self.token},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Gotify is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc
        if resp.is_error:
            raise UpstreamError(
                "validation" if resp.status_code < 500 else "upstream_unreachable",
                "Gotify rejected the request." if resp.status_code < 500 else "Gotify error.",
                {"status_code": resp.status_code},
            )
        data: Any = resp.json()
        msgs = data.get("messages", []) if isinstance(data, dict) else []
        return [
            {
                "title": m.get("title"),
                "message": m.get("message"),
                "priority": m.get("priority"),
                "date": m.get("date"),
            }
            for m in msgs
        ]
