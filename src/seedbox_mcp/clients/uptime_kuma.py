from __future__ import annotations

import re
from typing import Any

import httpx

from seedbox_mcp.errors import UpstreamError

# Uptime Kuma exposes per-monitor up/down at /metrics (Prometheus text),
# authenticated with an API key as the HTTP Basic password (blank username).
# One GET returns the whole fleet — every cluster node and every service in a
# single shot — which is exactly the "is everything up across the NAS, not
# just Plex" view the bot needs. Status codes: 1=up, 0=down, 2=pending,
# 3=maintenance.
_STATUS_RE = re.compile(r"^monitor_status\{(?P<labels>[^}]*)\}\s+(?P<status>\d+)")
_NAME_RE = re.compile(r'monitor_name="([^"]*)"')
_TYPE_RE = re.compile(r'monitor_type="([^"]*)"')
_STATUS_LABEL = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}


def parse_fleet_metrics(text: str) -> list[dict[str, Any]]:
    """Pure. Parse Uptime Kuma /metrics text into per-monitor status dicts."""
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        m = _STATUS_RE.match(raw.strip())
        if not m:
            continue
        labels = m.group("labels")
        name_m = _NAME_RE.search(labels)
        type_m = _TYPE_RE.search(labels)
        status = int(m.group("status"))
        out.append(
            {
                "name": name_m.group(1) if name_m else "?",
                "type": type_m.group(1) if type_m else "?",
                "status": status,
                "status_label": _STATUS_LABEL.get(status, f"unknown({status})"),
                "up": status == 1,
            }
        )
    return out


class UptimeKumaClient:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    async def fleet_status(self) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.url}/metrics", auth=("", self.api_key))
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Uptime Kuma is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc
        if resp.is_error:
            raise UpstreamError(
                "validation" if resp.status_code < 500 else "upstream_unreachable",
                "Uptime Kuma rejected the request."
                if resp.status_code < 500
                else "Uptime Kuma error.",
                {"status_code": resp.status_code},
            )
        return parse_fleet_metrics(resp.text)
