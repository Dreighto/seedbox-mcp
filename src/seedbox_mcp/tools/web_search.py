from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


def _unavailable() -> dict[str, Any]:
    return ToolResponse.failure(
        "web_search_unavailable", "Web search is not configured (OLLAMA_WEB_SEARCH_API_KEY unset)."
    )


async def web_search(services: Services, query: str, max_results: int = 5) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.ollama_web:
            return _unavailable()
        bounded = max(1, min(max_results, 10))
        data = await services.ollama_web.search(query, bounded)
        results = data.get("results", []) if isinstance(data, dict) else []
        return ToolResponse.success(
            {
                "query": query,
                "results": [
                    {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")} for r in results
                ],
            }
        )

    return await safe_tool(run)


async def web_fetch(services: Services, url: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.ollama_web:
            return _unavailable()
        data = await services.ollama_web.fetch(url)
        content = data.get("content", "") if isinstance(data, dict) else ""
        return ToolResponse.success(
            {
                "url": url,
                "title": data.get("title") if isinstance(data, dict) else None,
                # Full pages can be enormous — this is meant for "read one
                # article/doc the operator or a search result pointed at",
                # not for ingesting an entire site. Truncate rather than
                # blowing the model's context on one fetch.
                "content": content[:8000],
                "truncated": len(content) > 8000,
            }
        )

    return await safe_tool(run)
