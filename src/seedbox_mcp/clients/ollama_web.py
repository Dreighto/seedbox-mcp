from __future__ import annotations

from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError

# Separate from the local-daemon-to-cloud-model path (chat/ollama_ai.py talks
# to 127.0.0.1:11434, which proxies :cloud model inference under the
# operator's signed-in Ollama Pro session) — web search/fetch are hosted
# REST endpoints on ollama.com itself, gated by a distinct API key generated
# at ollama.com/settings/keys, not the `ollama signin` OAuth session. The
# local daemon does not proxy these (confirmed: 127.0.0.1:11434/api/web_search
# 404s) — always call ollama.com directly.
OLLAMA_WEB_BASE_URL = "https://ollama.com"


class OllamaWebClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        return await self._post("/api/web_search", {"query": query, "max_results": max_results})

    async def fetch(self, url: str) -> dict[str, Any]:
        return await self._post("/api/web_fetch", {"url": url})

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(f"{OLLAMA_WEB_BASE_URL}{path}", headers=self.headers, json=body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable", "ollama.com is unreachable.", {"path": path, "reason": exc.__class__.__name__}
            ) from exc
        if response.is_error:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise UpstreamError(
                "validation" if response.status_code < 500 else "upstream_unreachable",
                "Ollama web API rejected the request." if response.status_code < 500 else "Ollama web API error.",
                {"path": path, "status_code": response.status_code, "body": detail},
            )
        return cast(dict[str, Any], response.json())
