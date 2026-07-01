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
        result = await services.nasdoom.post("/v1/omni/add", body)
        return ToolResponse.success({"dry_run": False, **result})

    return await safe_tool(run)


async def nasdoom_profiles(services: Services, kind: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.nasdoom:
            return _unavailable()
        if kind not in VALID_ADD_KINDS:
            return ToolResponse.failure("validation", "Unsupported kind.", {"allowed": sorted(VALID_ADD_KINDS)})
        return ToolResponse.success(await services.nasdoom.get("/v1/profiles", {"kind": kind}))

    return await safe_tool(run)
