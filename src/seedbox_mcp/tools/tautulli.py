from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import clamp_limit, compact_tautulli_activity, safe_tool

_GROUPING = {"daily": 0, "monthly": 1, "total": 2}


async def tautulli_history(
    services: Services,
    user: str | None = None,
    rating_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    media_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.tautulli:
            return ToolResponse.failure("tautulli_unavailable", "Tautulli is not configured.")
        bounded = clamp_limit(limit)
        raw = await services.tautulli.get_history(
            limit=bounded,
            user=user,
            rating_key=rating_key,
            start_date=start_date,
            end_date=end_date,
            media_type=media_type,
        )
        records = raw.get("data", []) if isinstance(raw, dict) else raw
        total = raw.get("recordsFiltered") if isinstance(raw, dict) else len(records)
        return ToolResponse.success(
            {
                "total": total,
                "limit": bounded,
                "history": [_compact_history(item) for item in records if isinstance(item, dict)],
            }
        )

    return await safe_tool(run)


async def tautulli_users(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.tautulli:
            return ToolResponse.failure("tautulli_unavailable", "Tautulli is not configured.")
        raw = await services.tautulli.get_users()
        users: list[dict[str, Any]] = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        return ToolResponse.success({"users": [_compact_user(u) for u in users]})

    return await safe_tool(run)


async def tautulli_now_playing(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.tautulli:
            return ToolResponse.failure("tautulli_unavailable", "Tautulli is not configured.")
        activity = compact_tautulli_activity(await services.tautulli.get_activity())
        sessions = activity.get("sessions", [])
        stream_count = activity.get("stream_count")
        return ToolResponse.success(
            {
                "stream_count": len(sessions) if stream_count in (None, "") else stream_count,
                "total_bandwidth_kbps": activity.get("total_bandwidth"),
                "now_playing": [
                    {
                        "user": s.get("user"),
                        "title": s.get("grandparent_title") or s.get("title"),
                        "detail": s.get("title") if s.get("grandparent_title") else None,
                        "state": s.get("state"),
                        "progress_percent": s.get("progress_percent"),
                        "player": s.get("player"),
                        "transcode_decision": s.get("transcode_decision"),
                    }
                    for s in sessions
                ],
            }
        )

    return await safe_tool(run)


async def tautulli_user_stats(
    services: Services,
    user_id: int | None = None,
    grouping: str = "monthly",
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.tautulli:
            return ToolResponse.failure("tautulli_unavailable", "Tautulli is not configured.")
        raw = await services.tautulli.get_user_stats(
            user_id=user_id,
            grouping=_GROUPING.get(grouping, 1),
        )
        stats = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else raw
        return ToolResponse.success({"grouping": grouping, "user_id": user_id, "stats": stats})

    return await safe_tool(run)


def _compact_history(item: dict[str, Any]) -> dict[str, Any]:
    grandparent = item.get("grandparent_rating_key") or None
    result: dict[str, Any] = {
        "user": item.get("friendly_name") or item.get("user"),
        "media_type": item.get("media_type"),
        "title": item.get("full_title"),
        "year": item.get("year") or None,
        "rating_key": item.get("rating_key"),
        "started_at": _from_ts(item.get("date")),
        "duration_seconds": item.get("duration"),
        "percent_complete": item.get("percent_complete"),
        "watched_status": item.get("watched_status"),
        "player": item.get("player"),
        "platform": item.get("platform"),
    }
    if grandparent:
        result["grandparent_rating_key"] = grandparent
    return {k: v for k, v in result.items() if v is not None}


def _compact_user(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "user_id": item.get("user_id"),
        "username": item.get("username"),
        "friendly_name": item.get("friendly_name"),
        "email": item.get("email"),
        "is_active": bool(item.get("is_active", True)),
        "last_seen": _from_ts(item.get("last_seen")),
    }
    return {k: v for k, v in result.items() if v is not None}


def _from_ts(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except (ValueError, TypeError, OSError):
        return None
