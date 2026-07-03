from __future__ import annotations

from typing import Any

import httpx

from seedbox_mcp.errors import UpstreamError


def _flatten_top(items: list[dict[str, Any]] | None, limit: int = 5) -> list[dict[str, Any]]:
    """AdGuard returns top-lists as [{"domain-or-ip": count}, ...]. Flatten to
    [{"name": ..., "count": ...}] and cap."""
    out: list[dict[str, Any]] = []
    for entry in items or []:
        for name, count in entry.items():
            out.append({"name": name, "count": count})
    return out[:limit]


def summarize_stats(stats: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """Pure. Fold AdGuard /control/stats + /control/status into a compact
    health rollup the bot can report."""
    queries = stats.get("num_dns_queries", 0) or 0
    blocked = stats.get("num_blocked_filtering", 0) or 0
    return {
        "protection_enabled": status.get("protection_enabled"),
        "running": status.get("running"),
        "queries": queries,
        "blocked": blocked,
        "block_rate_pct": round(100 * blocked / queries, 1) if queries else 0.0,
        "avg_processing_ms": round((stats.get("avg_processing_time") or 0) * 1000, 1),
        "top_blocked_domains": _flatten_top(stats.get("top_blocked_domains")),
        "top_clients": _flatten_top(stats.get("top_clients")),
    }


class AdGuardClient:
    def __init__(self, url: str, username: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.auth = (username, password)

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        resp = await client.get(f"{self.url}{path}", auth=self.auth)
        if resp.is_error:
            raise UpstreamError(
                "validation" if resp.status_code < 500 else "upstream_unreachable",
                "AdGuard rejected the request."
                if resp.status_code < 500
                else "AdGuard error.",
                {"status_code": resp.status_code, "path": path},
            )
        data: Any = resp.json()
        return data if isinstance(data, dict) else {}

    async def stats_summary(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                status = await self._get(client, "/control/status")
                stats = await self._get(client, "/control/stats")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "AdGuard is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc
        return summarize_stats(stats, status)
