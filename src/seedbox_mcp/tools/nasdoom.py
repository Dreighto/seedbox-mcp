from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


def _unavailable() -> dict[str, Any]:
    return ToolResponse.failure("nasdoom_unavailable", "NASDOOM BFF is not configured.")


async def nasdoom_health(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/health"))

    return await safe_tool(run)


async def nasdoom_queue(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/queue"))

    return await safe_tool(run)


async def nasdoom_omni_search(services: Services, query: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/omni", {"q": query}))

    return await safe_tool(run)


async def nasdoom_requests_overview(services: Services, filter: str = "pending", take: int = 20) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/requests", {"filter": filter, "take": take}))

    return await safe_tool(run)


async def nasdoom_control(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/control"))

    return await safe_tool(run)


# ── Tier 1 actions — reversible, low-stakes, safe to execute directly ──────

VALID_GLOBAL_QUEUE_ACTIONS = {"pause", "resume", "speedcap"}
VALID_ITEM_QUEUE_ACTIONS = {"pause", "resume", "cancel", "priority"}
VALID_REQUEST_ACTIONS = {"approve", "decline"}


async def nasdoom_queue_command(
    services: Services, action: str, value: float | None = None, unit: str | None = None
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if action not in VALID_GLOBAL_QUEUE_ACTIONS:
            return ToolResponse.failure(
                "validation", "Unsupported action.", {"allowed": sorted(VALID_GLOBAL_QUEUE_ACTIONS)}
            )
        body: dict[str, Any] = {"action": action}
        if value is not None:
            body["value"] = value
        if unit is not None:
            body["unit"] = unit
        return ToolResponse.success(await services.nasdoom.post("/v1/queue/command", body))

    return await safe_tool(run)


async def nasdoom_queue_item_command(
    services: Services, item_id: str, action: str, value: float | None = None
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if action not in VALID_ITEM_QUEUE_ACTIONS:
            return ToolResponse.failure(
                "validation", "Unsupported action.", {"allowed": sorted(VALID_ITEM_QUEUE_ACTIONS)}
            )
        body: dict[str, Any] = {"action": action}
        if value is not None:
            body["value"] = value
        return ToolResponse.success(await services.nasdoom.post(f"/v1/queue/{item_id}/command", body))

    return await safe_tool(run)


async def nasdoom_requests_action(services: Services, request_id: str, action: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if action not in VALID_REQUEST_ACTIONS:
            return ToolResponse.failure("validation", "Unsupported action.", {"allowed": sorted(VALID_REQUEST_ACTIONS)})
        return ToolResponse.success(await services.nasdoom.post(f"/v1/requests/{request_id}/{action}"))

    return await safe_tool(run)


async def nasdoom_match_search(services: Services, rating_key: str, query: str | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        params = {"query": query} if query else None
        return ToolResponse.success(await services.nasdoom.get(f"/v1/match/{rating_key}", params))

    return await safe_tool(run)


async def nasdoom_match_apply(services: Services, rating_key: str, guid: str, name: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(
            await services.nasdoom.post(f"/v1/match/{rating_key}", {"guid": guid, "name": name})
        )

    return await safe_tool(run)
