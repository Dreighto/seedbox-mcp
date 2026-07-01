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
    services: Services,
    action: str,
    value: float | None = None,
    unit: str | None = None,
    confirm: bool = False,
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
        if not confirm:
            current = await services.nasdoom.get("/v1/queue")
            return ToolResponse.success(
                {"dry_run": True, "current_state": current.get("global"), "would_apply": body}
            )
        return ToolResponse.success({"dry_run": False, **await services.nasdoom.post("/v1/queue/command", body)})

    return await safe_tool(run)


async def nasdoom_queue_item_command(
    services: Services, item_id: str, action: str, value: float | None = None, confirm: bool = False
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
        if not confirm:
            queue = await services.nasdoom.get("/v1/queue")
            items = queue.get("items", []) if isinstance(queue, dict) else []
            current_item = next((i for i in items if i.get("id") == item_id), None)
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "current_item": current_item,
                    "item_found": current_item is not None,
                    "would_apply": {"item_id": item_id, **body},
                }
            )
        return ToolResponse.success(
            {"dry_run": False, **await services.nasdoom.post(f"/v1/queue/{item_id}/command", body)}
        )

    return await safe_tool(run)


async def nasdoom_requests_action(
    services: Services, request_id: str, action: str, confirm: bool = False
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if action not in VALID_REQUEST_ACTIONS:
            return ToolResponse.failure(
                "validation", "Unsupported action.", {"allowed": sorted(VALID_REQUEST_ACTIONS)}
            )
        if not confirm:
            # Look the request up so the preview shows what's actually being
            # approved/declined (title, requester) rather than a bare ID the
            # model could have gotten wrong.
            listing = await services.nasdoom.get("/v1/requests", {"filter": "all", "take": 100})
            requests = listing.get("requests", []) if isinstance(listing, dict) else []
            matched = next((r for r in requests if str(r.get("id")) == str(request_id)), None)
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "matched_request": matched,
                    "request_found": matched is not None,
                    "would_apply": {"request_id": request_id, "action": action},
                }
            )
        return ToolResponse.success(
            {"dry_run": False, **await services.nasdoom.post(f"/v1/requests/{request_id}/{action}")}
        )

    return await safe_tool(run)


async def nasdoom_match_search(services: Services, rating_key: str, query: str | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        params = {"query": query} if query else None
        return ToolResponse.success(await services.nasdoom.get(f"/v1/match/{rating_key}", params))

    return await safe_tool(run)


async def nasdoom_match_apply(
    services: Services, rating_key: str, guid: str, name: str, confirm: bool = False
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if not confirm:
            return ToolResponse.success(
                {"dry_run": True, "would_apply": {"rating_key": rating_key, "guid": guid, "name": name}}
            )
        return ToolResponse.success(
            {"dry_run": False, **await services.nasdoom.post(f"/v1/match/{rating_key}", {"guid": guid, "name": name})}
        )

    return await safe_tool(run)
