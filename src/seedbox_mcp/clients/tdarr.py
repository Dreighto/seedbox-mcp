from __future__ import annotations

from typing import Any

import httpx

from seedbox_mcp.errors import UpstreamError

_STATS_QUERY = {
    "data": {"collection": "StatisticsJSONDB", "mode": "getAll", "docID": "", "obj": {}}
}


def summarize_tdarr(nodes_data: Any, stats_data: Any) -> dict[str, Any]:
    """Pure. Fold Tdarr get-nodes + StatisticsJSONDB into a status rollup.
    space_saved reflects only files Tdarr itself processed (out-of-band
    re-encodes don't count)."""
    nodes = []
    for n in nodes_data.values() if isinstance(nodes_data, dict) else []:
        if not isinstance(n, dict):
            continue
        nodes.append(
            {
                "name": n.get("nodeName"),
                "paused": n.get("nodePaused"),
                "workers": len(n.get("workers") or {}),
            }
        )
    stats: dict[str, Any] = {}
    if isinstance(stats_data, list) and stats_data and isinstance(stats_data[0], dict):
        stats = stats_data[0]
    elif isinstance(stats_data, dict):
        stats = stats_data
    size = stats.get("sizeDiff") or 0
    return {
        "nodes": nodes,
        "any_processing": any((not n["paused"]) or n["workers"] > 0 for n in nodes),
        "space_saved_gb": round(size / 1e9, 1),
        "lifetime_transcodes": stats.get("totalTranscodeCount", 0),
        "lifetime_health_checks": stats.get("totalHealthCheckCount", 0),
        "files_in_library": stats.get("table1Count", 0),
    }


class TdarrClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                nodes_r = await client.get(f"{self.url}/api/v2/get-nodes")
                stats_r = await client.post(f"{self.url}/api/v2/cruddb", json=_STATS_QUERY)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Tdarr is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc
        for r in (nodes_r, stats_r):
            if r.is_error:
                raise UpstreamError(
                    "validation" if r.status_code < 500 else "upstream_unreachable",
                    "Tdarr rejected the request." if r.status_code < 500 else "Tdarr error.",
                    {"status_code": r.status_code},
                )
        return summarize_tdarr(nodes_r.json(), stats_r.json())
