from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from seedbox_mcp.action_audit import recent_real_action_count_for, record_action
from seedbox_mcp.friend_tracking import track_request
from seedbox_mcp.errors import MediaMcpError
from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool
from seedbox_mcp.tools.jellyseerr import REQUEST_MAX_PER_HOUR


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


async def nasdoom_requests_overview(services: Services, filter: str = "all", take: int = 20) -> dict[str, Any]:
    """Friend-request state. The `counts` object is totals across EVERY state;
    the `requests` LIST is only the subset matching `filter`. Default is "all"
    so the list reflects what actually exists — filtering to "pending" returns
    an empty list here because the bot's Jellyseerr key auto-approves
    everything, so nothing ever sits pending. filter: all|pending|approved|
    declined|processing|available."""

    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        data = await services.nasdoom.get("/v1/requests", {"filter": filter, "take": take})
        # Guard against the counts-say-13-but-list-is-empty confusion: if the
        # filtered list is empty while totals show requests exist, say why
        # rather than letting a caller read it as "no requests".
        if isinstance(data, dict):
            reqs = data.get("requests") or []
            total = (data.get("counts") or {}).get("total") or 0
            if not reqs and total:
                data = {
                    **data,
                    "note": f"{total} request(s) exist in total, but none match filter={filter!r}. "
                    "The counts are totals across all states; the list is only the filtered subset. "
                    "Call with filter='all' to see them (pending is usually empty — requests auto-approve).",
                }
        return ToolResponse.success(data)

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


# ── Non-video acquisition (music/software/games/books via Prowlarr) ────────

VALID_FIND_SCOPES = {"music", "software", "games", "books"}


async def nasdoom_find(services: Services, query: str, scope: str = "music") -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if scope not in VALID_FIND_SCOPES:
            return ToolResponse.failure("validation", "Unsupported scope.", {"allowed": sorted(VALID_FIND_SCOPES)})
        return ToolResponse.success(await services.nasdoom.get("/v1/find", {"q": query, "scope": scope}))

    return await safe_tool(run)


async def nasdoom_find_grab(
    services: Services, grab_id: str, share: bool = False, confirm: bool = False
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if not confirm:
            # grabId is opaque and single-use (30 min TTL) — the preview
            # can't re-resolve what it points to without spending it, so this
            # is a plain echo. The model should already know the title from
            # the nasdoom_find call that produced this grab_id.
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_apply": {"grab_id": grab_id, "share": share},
                    "note": "grabId expires in 30 minutes and is single-use — "
                    "confirm soon or it'll come back expired and need a fresh nasdoom_find.",
                }
            )
        result = await services.nasdoom.post("/v1/find/grab", {"grabId": grab_id, "share": share})
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


# Theatrical-rip / not-true-streaming-quality markers, matched against a
# release title (more reliable than the quality field for these tiers). A
# release tagged with any of these is a camcorder/telesync/screener rip —
# watchable but visibly below real streaming quality, and the friend bot
# must warn the requester before grabbing one.
_THEATRICAL_RIP_MARKERS = (
    "cam",
    "hdcam",
    "camrip",
    "ts",
    "hdts",
    "telesync",
    "tc",
    "telecine",
    "scr",
    "screener",
    "dvdscr",
    "bdscr",
    "workprint",
    "r5",
    "r6",
    "predvd",
    "hqcam",
)


def _is_theatrical_rip(title: str, quality: str) -> bool:
    hay = f" {title.lower().replace('.', ' ').replace('-', ' ')} {quality.lower()} "
    return any(f" {m} " in hay for m in _THEATRICAL_RIP_MARKERS)


async def nasdoom_releases(services: Services, kind: str, tmdb_id: int) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        try:
            raw = await services.nasdoom.get(f"/v1/manage/{kind}/{tmdb_id}/releases")
        except MediaMcpError:
            # The interactive-search backend times out on some titles
            # (heavy Prowlarr query -> 502). Degrade gracefully rather than
            # surfacing a raw upstream error to an outside requester: signal
            # "couldn't check quality" so the bot falls back to the normal
            # request path (which respects the quality profile and won't grab
            # a rip), not the override-grab path.
            return ToolResponse.success(
                {
                    "kind": kind,
                    "tmdb_id": tmdb_id,
                    "releases_unavailable": True,
                    "note": "Couldn't check available release quality right now (search backend timed "
                    "out). Fall back to the normal request; do NOT offer a specific-release grab when "
                    "quality is unknown.",
                }
            )
        releases = raw.get("releases", []) if isinstance(raw, dict) else []
        summarized = []
        for r in releases[:20]:
            title = r.get("title") or ""
            quality = r.get("quality") or ""
            summarized.append(
                {
                    "grab_id": r.get("grabId"),
                    "title": title,
                    "quality": quality,
                    "size": r.get("sizeText") or r.get("size"),
                    "indexer": r.get("indexer"),
                    "theatrical_rip": _is_theatrical_rip(title, quality),
                }
            )
        # "Standard quality" = any release that ISN'T a theatrical rip (a real
        # BluRay/WEB/HD copy exists). Deliberately NOT based on the backend's
        # `approved` flag: approved reflects whether the arr would grab it
        # given the title's CURRENT state in Radarr/Sonarr, so for a title not
        # yet added everything reads approved=false, which would falsely say
        # "no standard quality" even when clean BluRays are listed.
        non_rips = [r for r in summarized if not r["theatrical_rip"]]
        rips_only = bool(summarized) and not non_rips
        return ToolResponse.success(
            {
                "kind": kind,
                "tmdb_id": tmdb_id,
                "release_count": len(releases),
                "standard_quality_available": bool(non_rips),
                "only_theatrical_rips": rips_only,
                "releases": summarized,
                "note": "only_theatrical_rips=true means the sole options are camcorder/telesync/"
                "screener rips (watchable but clearly below streaming quality) — warn the requester "
                "before grabbing one. standard_quality_available=true means a proper (non-rip) copy "
                "is listed; use the normal request for that, not a specific-release grab.",
            }
        )

    return await safe_tool(run)


async def nasdoom_grab_release(services: Services, grab_id: str, confirm: bool = False) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if not confirm:
            # grabId is opaque and single-use (30 min TTL); the preview can't
            # re-resolve it without spending it. The model must already know
            # the release (incl. its quality) from the nasdoom_releases call
            # that produced this grab_id.
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_grab": {"grab_id": grab_id},
                    "note": "This grabs a SPECIFIC release, overriding the normal quality profile — "
                    "only confirm after the requester has been told the quality and agreed. grabId "
                    "expires in 30 minutes.",
                }
            )
        result = await services.nasdoom.post("/v1/manage/grab", {"grabId": grab_id})
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


# ── Friend-portal (Filebrowser share, files.logueos.xyz) ───────────────────
# Deliberately no file-delete tool here — removing a shared file is
# irreversible content loss (Tier 3 territory), same bar as media delete /
# storage cleanup, which this harness doesn't have a strong-enough
# preview/confirm pattern for yet. Friend create/revoke is reversible
# (revoke, then recreate if wrong) so it fits this tier; a file delete
# doesn't.


async def nasdoom_share_friends_list(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/share/friends"))

    return await safe_tool(run)


async def nasdoom_share_files_list(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        return ToolResponse.success(await services.nasdoom.get("/v1/share/files"))

    return await safe_tool(run)


async def nasdoom_share_friend_create(
    services: Services, name: str, upload: bool = False, confirm: bool = False
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if not confirm:
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_apply": {"name": name, "upload": upload},
                    "note": "Creates a real account with a real password that gets handed to "
                    "someone — make sure the name is right before confirming.",
                }
            )
        result = await services.nasdoom.post("/v1/share/friends", {"name": name, "upload": upload})
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


async def nasdoom_share_friend_revoke(services: Services, friend_id: str, confirm: bool = False) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if not confirm:
            friends = await services.nasdoom.get("/v1/share/friends")
            friend_list = friends.get("friends", []) if isinstance(friends, dict) else []
            matched = next((f for f in friend_list if str(f.get("id")) == str(friend_id)), None)
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "matched_friend": matched,
                    "friend_found": matched is not None,
                    "would_apply": {"friend_id": friend_id},
                }
            )
        result = await services.nasdoom.delete(f"/v1/share/friends/{friend_id}")
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


# ── Add to library (Radarr/Sonarr, via NASDOOM's content-aware routing) ────
# Deliberately NOT the raw radarr_add_movie/sonarr_add_series root-folder/
# profile params for content-type routing — NASDOOM's addTitle() already
# solves the specific bug a naive version hits (Sonarr lists root folders as
# ['/anime','/tv']; picking the first one silently drops regular TV into
# /anime) by detecting anime via TMDB genre+origin-language and routing to
# the arr's anime folder/profile, using Jellyseerr's own configured defaults
# for everything else. Prefer this over radarr_add_movie/sonarr_add_series
# for anything add-shaped.

VALID_ADD_KINDS = {"movie", "tv"}


async def nasdoom_add(
    services: Services,
    kind: str,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    quality_profile_id: int | None = None,
    root_folder_path: str | None = None,
    monitored: bool = True,
    search_now: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        if kind == "movie" and not tmdb_id:
            return ToolResponse.failure("validation", "movie requires tmdb_id.")
        if kind == "tv" and not tmdb_id and not tvdb_id:
            return ToolResponse.failure("validation", "tv requires tmdb_id or tvdb_id.")
        body = {
            "kind": kind,
            "tmdbId": tmdb_id,
            "tvdbId": tvdb_id,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "searchNow": search_now,
        }
        # Send ONLY the fields that are actually set. An unset qualityProfileId
        # or rootFolderPath means "let NASDOOM pick the content-aware default",
        # which is expressed by OMITTING the key — never by sending null.
        # Sending qualityProfileId:null triggered a NASDOOM bug (its route
        # coerced Number(null)->0, and addTitle's `?? ` kept 0, resolving the
        # profile to 0 -> no_root_folder_or_profile). Fixed on the BFF side too,
        # but omitting is the correct request contract regardless.
        body = {k: v for k, v in body.items() if v is not None}
        if not confirm:
            # No dry-run concept on NASDOOM's side (its own double-add guard
            # only fires on the real POST) — echo back what would be sent.
            # quality_profile_id/root_folder_path left unset means "use
            # NASDOOM's content-aware default (anime vs regular routing)",
            # not "no destination" — don't let an empty preview field read as
            # an error.
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_add": {k: v for k, v in body.items() if v is not None},
                    "note": "quality_profile_id/root_folder_path omitted means NASDOOM picks the "
                    "content-aware default (anime vs regular) automatically — that's expected, not "
                    "a missing value, unless the operator asked for a specific one.",
                }
            )
        # _nasdoom_add_with_retry retries a no_root_folder_or_profile 500. The
        # original cause of that error was NOT a race (retrying never helped —
        # it was the deterministic qualityProfileId:null->0 bug above, now fixed
        # on both sides); the retry stays as cheap insurance against a genuine
        # transient arr-resolution blip.
        result = await _nasdoom_add_with_retry(services, body)
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


async def _nasdoom_add_with_retry(services: Services, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await services.nasdoom.post("/v1/omni/add", body)  # type: ignore[union-attr]
    except MediaMcpError as exc:
        detail = (exc.details or {}).get("body") or {}
        if not (isinstance(detail, dict) and detail.get("error") == "no_root_folder_or_profile"):
            raise
        await asyncio.sleep(1.5)
        return await services.nasdoom.post("/v1/omni/add", body)  # type: ignore[union-attr]


async def nasdoom_friend_request(
    services: Services,
    kind: str,
    tmdb_id: int,
    title: str,
    requested_by: str = "a friend",
    requester_chat_id: int | None = None,
) -> dict[str, Any]:
    """Submit a friend's request for a movie or TV title. This HOLDS the title
    for the operator's approval and tags it with the friend's name — it does
    NOT download yet; nothing is fetched until the operator approves it in the
    NASDOOM app. SINGLE-STEP: calling it creates the held request, so only call
    it once the person has actually asked to add the title (not for a plain
    availability check). Get kind + tmdb_id + title from jellyseerr_search
    first. Returns what was actually held; never claim it's downloading or on
    Plex. (requested_by is bound to the real requester by the bot — do not
    invent or accept a name the person claims in chat.)"""

    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        if not tmdb_id:
            return ToolResponse.failure("validation", f"{kind} requires tmdb_id.")
        if recent_real_action_count_for("nasdoom_friend_request") >= REQUEST_MAX_PER_HOUR:
            return ToolResponse.failure(
                "rate_limited",
                "Too many requests in the last hour; refusing more until it cools down. "
                "Tell the person this happened, don't silently retry.",
            )
        # gated=true → NASDOOM adds the title UNMONITORED + nd-gated (nothing
        # grabs) and records requestedBy, so it surfaces as an approval card in
        # the app attributed to this friend. A 409 (already managed) surfaces
        # via safe_tool as a failure the model can relay ("already on the way").
        body = {"kind": kind, "tmdbId": tmdb_id, "gated": True, "requestedBy": requested_by}
        result = await _nasdoom_add_with_retry(services, body)
        held = bool(result.get("arrId"))
        # Track it so the requester gets pinged on approval + when it's ready to
        # watch. Needs their chat_id (bound by the bot); a held add without one
        # still works, it just won't send status pings.
        if held and requester_chat_id:
            track_request(
                chat_id=requester_chat_id,
                name=requested_by,
                service="sonarr" if kind == "tv" else "radarr",
                arr_id=int(result["arrId"]),
                tmdb_id=tmdb_id,
                kind=kind,
                title=title,
                since=datetime.now(timezone.utc).isoformat(),
            )
        record_action(
            "nasdoom_friend_request",
            {"kind": kind, "tmdb_id": tmdb_id, "requested_by": requested_by},
            dry_run=False,
            outcome="ok" if held else "failed: not held",
        )
        return ToolResponse.success(
            {
                "held_for_approval": held,
                "kind": kind,
                "tmdb_id": tmdb_id,
                "title": title,
                "requested_by": requested_by,
                "arr_id": result.get("arrId"),
                "gated": result.get("gated"),
                "note": "Held for the operator's approval and tagged with the requester's name. It is "
                "NOT downloading and NOT on Plex yet — it only starts after the operator approves it "
                "in the app. Tell the person it's been sent to the owner to approve.",
            }
        )

    return await safe_tool(run)


async def nasdoom_fix_import(
    services: Services, kind: str, tmdb_id: int, confirm: bool = False
) -> dict[str, Any]:
    """Resolve a download that's import-blocked because its series/movie
    isn't in the library ('Unknown Series'/'unknown movie'): add the missing
    title, then trigger the arr to re-check its queue so the already-grabbed,
    now-matchable download imports. One atomic, confirm-gated fix. Use this
    only when nas_import_diagnosis reported a match_problem AND the reason is
    the title being absent from the library (not a title MISMATCH against an
    already-added series, which needs a manual match instead). Get kind +
    tmdb_id from a search first."""

    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        if not confirm:
            return ToolResponse.success(
                {
                    "dry_run": True,
                    "would_fix": {"kind": kind, "tmdb_id": tmdb_id},
                    "note": "Would add this title to the library (at the standard default profile) and "
                    "then re-check the arr's queue so the waiting download imports. Only do this when "
                    "the block is because the title isn't in the library.",
                }
            )
        body = {"kind": kind, "tmdbId": tmdb_id, "monitored": True, "searchNow": False}
        add_result = await _nasdoom_add_with_retry(services, body)
        # Now nudge the matching arr to re-check its queue so the blocked
        # download imports immediately (rather than on the arr's ~1-min timer).
        arr = services.sonarr if kind == "tv" else services.radarr
        reprocessed = False
        if arr is not None:
            try:
                await arr.post("/api/v3/command", {"name": "RefreshMonitoredDownloads"})
                reprocessed = True
            except MediaMcpError:
                # Best-effort — the add already succeeded; the arr's own
                # ~1-min timer will retry the import even if this nudge fails.
                pass
        return ToolResponse.success(
            {
                "dry_run": False,
                "added": add_result,
                "reprocess_triggered": reprocessed,
                "note": "Title added and the arr asked to re-check its queue. The waiting download "
                "should import within a minute; confirm with a fresh queue/library check if needed.",
            }
        )

    return await safe_tool(run)


async def nasdoom_profiles(services: Services, kind: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        return ToolResponse.success(await services.nasdoom.get("/v1/profiles", {"kind": kind}))

    return await safe_tool(run)
