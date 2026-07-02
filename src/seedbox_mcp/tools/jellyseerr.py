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

# Jellyseerr MediaStatus enum. The distinction that matters for honesty:
# only AVAILABLE (5) means actually watchable on Plex right now. Everything
# else — pending approval, approved, even a watchlist-only record — is NOT
# streamable yet, and must never be reported as "on Plex".
# PARTIALLY_AVAILABLE (4) is for a TV series with some but not all episodes.
# Status 3 (PROCESSING) is deliberately NOT mapped here: it needs the
# downloadStatus array to tell "actively downloading" apart from "approved
# and waiting for a copy to exist" (see _availability). Reporting the second
# as "downloading now" is misleading — for an unreleased title nothing is
# downloading at all, it's just monitored.
_MEDIA_STATUS = {
    1: "not_available",  # a record exists (e.g. watchlisted) but nothing is downloaded
    2: "requested_pending_approval",
    4: "partially_available",
    5: "available",  # actually on Plex, watchable now
}


def _availability(result: dict[str, Any]) -> str:
    """Precise streamable state from Jellyseerr's mediaInfo. No mediaInfo at
    all → nothing in the system yet (fully requestable)."""
    mi = result.get("mediaInfo")
    if not mi:
        return "not_in_library"
    status = mi.get("status")
    if status == 3:
        # PROCESSING: only call it "downloading" if the download-client
        # status array actually has an active item. Empty means it's
        # approved/monitored and waiting for a release to appear (e.g. the
        # movie isn't out yet) — nothing is downloading.
        active = mi.get("downloadStatus") or mi.get("downloadStatus4k")
        return "downloading" if active else "approved_waiting_for_release"
    return _MEDIA_STATUS.get(status, "not_available")


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
                # Precise, not a boolean: availability is one of
                # available / partially_available / downloading /
                # requested_pending_approval / not_available / not_in_library.
                # ONLY "available" (and "partially_available" for a series)
                # means it can actually be streamed right now.
                # available / partially_available / downloading /
                # approved_waiting_for_release / requested_pending_approval /
                # not_available / not_in_library. Only "available" (and
                # "partially_available" for a series) is streamable now.
                "availability": _availability(r),
                "streamable_now": _availability(r) in ("available", "partially_available"),
            }
            for r in filtered[:10]
        ]
        return ToolResponse.success(
            {
                "query": query,
                "titles": titles,
                "filtered_out": dropped,
                "note": "streamable_now / availability tell the truth about whether a title can "
                "actually be watched NOW. 'downloading' and 'requested_pending_approval' mean it is "
                "NOT on Plex yet, only on the way. Never tell someone a title is on Plex unless "
                "streamable_now is true.",
            }
        )

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
