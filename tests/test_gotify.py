from __future__ import annotations

import pytest

from seedbox_mcp.tools.gotify import gotify_alerts

MSGS = [
    {"title": "Uptime-Kuma", "message": "[Node: ailogueos] [Up]", "priority": 8, "date": "2026-07-02T18:03:15"},
    {"title": "Uptime-Kuma", "message": "[Node: ailogueos] [Down]", "priority": 8, "date": "2026-07-02T18:02:14"},
]


class _StubGotify:
    def __init__(self, msgs): self._m = msgs
    async def recent(self, limit=10): return self._m[:limit]


class _StubServices:
    def __init__(self, gotify): self.gotify = gotify


@pytest.mark.asyncio
async def test_gotify_alerts_returns_recent() -> None:
    res = await gotify_alerts(_StubServices(_StubGotify(MSGS)))  # type: ignore[arg-type]
    data = res.get("data", res)
    assert data["count"] == 2
    assert data["alerts"][0]["message"] == "[Node: ailogueos] [Up]"


@pytest.mark.asyncio
async def test_gotify_alerts_not_configured() -> None:
    res = await gotify_alerts(_StubServices(None))  # type: ignore[arg-type]
    assert "not_configured" in str(res)
