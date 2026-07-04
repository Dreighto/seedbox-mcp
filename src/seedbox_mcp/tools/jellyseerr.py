from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

# Jellyseerr's search is title-only and returns NOTHING if a year is tacked on
# ("Dream Eater 2025" → [], "The Godfather 1972" → []). The model naturally
# appends the year it knows, so strip a trailing year (bare or parenthesized)
# before searching; the year is still used to disambiguate the results after.
_TRAILING_YEAR_RE = re.compile(r"[\s(]+(?:19|20)\d{2}\)?\s*$")


def _clean_query(query: str) -> str:
    cleaned = _TRAILING_YEAR_RE.sub("", query).strip()
    return cleaned or query

from seedbox_mcp.action_audit import recent_real_action_count_for, record_action
from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

# Media requests are the friend bot's primary function, so they get their own
# generous rolling-hour cap instead of sharing the low system-action breaker
# (which is a runaway backstop for restarts etc.). Still a real ceiling so a
# confused or abusive requester can't fire hundreds, but high enough that
# normal use — a household of friends adding a watchlist — never hits it.
REQUEST_MAX_PER_HOUR = 60


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


_TMDB_IMG = "https://image.tmdb.org/t/p/w500"


def _poster(poster_path: Any) -> str | None:
    p = poster_path if isinstance(poster_path, str) else None
    return f"{_TMDB_IMG}{p}" if p and p.startswith("/") else None


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
        raw = await services.jellyseerr.get(f"/api/v1/search?query={quote(_clean_query(query))}")
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
                # Full TMDB poster URL so the bot can SHOW the title (posters
                # disambiguate way better than text). None when TMDB has no art.
                "poster": _poster(r.get("posterPath")),
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


async def jellyseerr_request_add(
    services: Services,
    kind: str,
    tmdb_id: int,
    title: str,
    year: int | None = None,
) -> dict[str, Any]:
    """Request a movie or TV series through Jellyseerr. SINGLE-STEP: calling
    this actually creates the request — there is no preview/confirm dance,
    so only call it once the person has clearly asked for the title to be
    added (not for a plain "is it on Plex" lookup). A TV series needs all
    seasons requested (Jellyseerr requires the seasons field, or the request
    is empty); this passes seasons="all". Get kind + tmdb_id + title from
    jellyseerr_search first — the id must come from a real search here, not
    memory. Returns the real Jellyseerr request id and the resulting state so
    the reply is grounded in what actually happened, never a claim before the
    call ran."""

    async def run() -> dict[str, Any]:
        if not services.jellyseerr:
            return _unavailable()
        if kind not in VALID_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_KINDS)})
        # Dedicated request breaker (not the shared system-action cap): a real
        # write exposed to outside users still gets a rolling-hour ceiling, but
        # a generous one sized for legitimate request volume.
        if recent_real_action_count_for("jellyseerr_request_add") >= REQUEST_MAX_PER_HOUR:
            return ToolResponse.failure(
                "rate_limited",
                "Too many requests in the last hour; refusing more until it cools down. "
                "Tell the person this happened, don't silently retry.",
            )
        # TV requires the seasons field or Jellyseerr creates an empty request
        # that downloads nothing; "all" is what a friend asking for "the show"
        # means. Movies take no seasons field.
        body: dict[str, Any] = {"mediaType": kind, "mediaId": tmdb_id}
        if kind == "tv":
            body["seasons"] = "all"
        result = await services.jellyseerr.post("/api/v1/request", body)
        req_id = result.get("id")
        media = result.get("media") or {}
        # Jellyseerr request status: 1=pending approval, 2=approved. The media
        # status (5=available, 3=processing/downloading, ...) says where the
        # underlying download is. Report what actually happened, plainly.
        req_status = result.get("status")
        state = "pending_approval" if req_status == 1 else "approved"
        record_action(
            "jellyseerr_request_add",
            {"kind": kind, "tmdb_id": tmdb_id},
            dry_run=False,
            outcome="ok" if req_id else "failed: no request id returned",
        )
        return ToolResponse.success(
            {
                "requested": bool(req_id),
                "kind": kind,
                "tmdb_id": tmdb_id,
                "title": title,
                "year": year,
                "jellyseerr_request_id": req_id,
                "state": state,
                "media_status": media.get("status"),
                "note": "Request created. state=approved means it's already being fetched; "
                "state=pending_approval means it's waiting on the owner. Do not tell the person "
                "it's on Plex — it isn't watchable until it finishes downloading.",
            }
        )

    return await safe_tool(run)
