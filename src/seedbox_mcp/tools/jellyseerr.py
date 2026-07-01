from __future__ import annotations

from typing import Any
from urllib.parse import quote

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.telegram import send_message
from seedbox_mcp.tools.common import safe_tool


def _unavailable() -> dict[str, Any]:
    return ToolResponse.failure("jellyseerr_unavailable", "Jellyseerr is not configured.")


VALID_KINDS = {"movie", "tv"}


def _year_from(result: dict[str, Any]) -> int | None:
    date = result.get("releaseDate") or result.get("firstAirDate")
    if date and len(str(date)) >= 4:
        try:
            return int(str(date)[:4])
        except ValueError:
            return None
    return None


async def jellyseerr_search(services: Services, query: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.jellyseerr:
            return _unavailable()
        # ArrClient's params= dict encodes spaces as "+", which Jellyseerr's
        # own validator rejects ("must be url encoded"). Pre-encode with
        # %20 and pass the whole querystring as the path instead.
        raw = await services.jellyseerr.get(f"/api/v1/search?query={quote(query)}")
        results = raw.get("results", []) if isinstance(raw, dict) else []
        # This is the direct Jellyseerr search, not NASDOOM's omni-search,
        # specifically because Jellyseerr's response carries TMDB's real
        # "adult" flag and NASDOOM's own OmniTitle shape doesn't expose one
        # to filter on. Anything flagged adult, or a person/other result
        # type, never reaches the model at all.
        filtered = [r for r in results if r.get("mediaType") in VALID_KINDS and not r.get("adult")]
        dropped = len(results) - len(filtered)
        titles = [
            {
                "kind": r["mediaType"],
                "title": r.get("title") or r.get("name"),
                "year": _year_from(r),
                "tmdb_id": r.get("id"),
                "overview": r.get("overview"),
                "already_in_library_or_requested": bool(r.get("mediaInfo")),
            }
            for r in filtered[:10]
        ]
        return ToolResponse.success({"query": query, "titles": titles, "filtered_out": dropped})

    return await safe_tool(run)


async def _notify_operator(services: Services, kind: str, tmdb_id: int, title: str, year: int | None) -> None:
    token = services.settings.nas_ops_telegram_bot_token
    chat_id = services.settings.nas_ops_telegram_allowed_chat_id
    if not token or not chat_id:
        return
    label = "TV series" if kind == "tv" else "movie (part of a multi-title request)"
    year_part = f" ({year})" if year else ""
    await send_message(
        token.get_secret_value(),
        chat_id,
        f'A friend request needs your review: {label} "{title}"{year_part} (tmdb {tmdb_id}). '
        "Add it yourself if you want it.",
    )


async def jellyseerr_request_add(
    services: Services,
    kind: str,
    tmdb_id: int,
    title: str,
    year: int | None = None,
    bulk: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """A single movie is added automatically — the operator's own stated
    tolerance is that movies are fine to add on their own. A TV series, or
    a movie the caller has flagged bulk=true (several titles requested in
    one message), is NOT sent to Jellyseerr. The API key here is the
    operator's own admin account, which auto-approves everything it
    creates, so there's no native pending-for-review state to rely on for
    those cases — the operator gets a direct Telegram notification and
    adds it themselves if they want it."""

    async def run() -> dict[str, Any]:
        if not services.jellyseerr:
            return _unavailable()
        if kind not in VALID_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_KINDS)})
        needs_operator_review = kind == "tv" or bulk
        if not confirm:
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_request": {"kind": kind, "tmdb_id": tmdb_id, "title": title, "year": year},
                    "routing": "operator_review" if needs_operator_review else "auto_add",
                }
            )
        if needs_operator_review:
            await _notify_operator(services, kind, tmdb_id, title, year)
            return ToolResponse.success(
                {
                    "dry_run": False,
                    "routed_to": "operator_review",
                    "note": "Sent to the operator to review and add themselves, not added automatically.",
                }
            )
        result = await services.jellyseerr.post("/api/v1/request", {"mediaType": kind, "mediaId": tmdb_id})
        return ToolResponse.success(
            {"dry_run": False, "routed_to": "auto_added", "jellyseerr_request_id": result.get("id")}
        )

    return await safe_tool(run)
