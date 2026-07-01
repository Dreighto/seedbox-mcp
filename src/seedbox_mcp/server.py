from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from seedbox_mcp.config import Settings, load_settings
from seedbox_mcp.oauth import OAuthStore
from seedbox_mcp.runtime import Services, build_services
from seedbox_mcp.tools.downloads import jellyseerr_overview, prowlarr_overview, sabnzbd_overview
from seedbox_mcp.tools.escalate import DEFAULT_ESCALATION_WORKER, DEFAULT_TARGET_REPO, escalate_to_worker
from seedbox_mcp.tools.nas_storage import nas_backup_health, nas_storage_inventory
from seedbox_mcp.tools.nasdoom import (
    nasdoom_control,
    nasdoom_find,
    nasdoom_find_grab,
    nasdoom_health,
    nasdoom_match_apply,
    nasdoom_match_search,
    nasdoom_omni_search,
    nasdoom_queue,
    nasdoom_queue_command,
    nasdoom_queue_item_command,
    nasdoom_requests_action,
    nasdoom_requests_overview,
    nasdoom_share_files_list,
    nasdoom_share_friend_create,
    nasdoom_share_friend_revoke,
    nasdoom_share_friends_list,
)
from seedbox_mcp.tools.plex import plex_library_size, plex_overview
from seedbox_mcp.tools.radarr import (
    radarr_add_movie,
    radarr_delete_movie,
    radarr_delete_movies_batch,
    radarr_overview,
    radarr_queue_action,
    radarr_research_movie,
)
from seedbox_mcp.tools.search import media_search
from seedbox_mcp.tools.sonarr import (
    sonarr_add_series,
    sonarr_delete_series,
    sonarr_delete_series_batch,
    sonarr_overview,
    sonarr_queue_action,
    sonarr_research_series,
)
from seedbox_mcp.tools.staleness import staleness_report
from seedbox_mcp.tools.status import media_status
from seedbox_mcp.tools.tautulli import tautulli_history, tautulli_user_stats, tautulli_users

logger = logging.getLogger("seedbox_mcp")

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


class BearerAuthApp:
    def __init__(self, app: ASGIApp, token: str, oauth_store: OAuthStore | None = None) -> None:
        self.app = app
        self.token = token
        self.oauth_store = oauth_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            response = JSONResponse({"ok": True})
            await response(scope, receive, send)
            return
        if path.startswith("/mcp") and not self._authorized(scope):
            response = JSONResponse(
                {
                    "ok": False,
                    "error_type": "upstream_auth",
                    "message": "Missing or invalid bearer token.",
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        import hmac as _hmac

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        candidate = auth[len("Bearer ") :] if auth.startswith("Bearer ") else auth
        if not candidate:
            return False
        if _hmac.compare_digest(candidate, self.token):
            return True
        if self.oauth_store is not None:
            return self.oauth_store.validate_access_token(candidate)
        return False


def create_mcp(services: Services) -> FastMCP:
    mcp = FastMCP("Seedbox MCP")

    async def media_status_tool() -> dict[str, Any]:
        return await media_status(services)

    async def radarr_overview_tool(
        include_movies: bool = True,
        include_queue: bool = True,
        include_missing: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Returns Radarr library state.

        Use include_queue=true to retrieve queue_id values needed by radarr_queue_action.
        Each queue item also carries radarr_id (for radarr_research_movie) plus a clean
        title and the raw release_title — act on these directly rather than feeding the
        release name back through media_search.
        Set include_movies=false or include_missing=false to reduce response size when
        only queue data is needed.
        """
        return await radarr_overview(services, include_movies, include_queue, include_missing, limit)

    async def sonarr_overview_tool(
        include_series: bool = True,
        include_queue: bool = True,
        include_missing: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Returns Sonarr library state.

        Use include_queue=true to retrieve queue_id values needed by sonarr_queue_action.
        Each queue item also carries sonarr_id (for sonarr_research_series) plus a clean
        title and the raw release_title — act on these directly rather than feeding the
        release name back through media_search.
        Set include_series=false or include_missing=false to reduce response size when
        only queue data is needed.
        """
        return await sonarr_overview(services, include_series, include_queue, include_missing, limit)

    async def plex_library_size_tool(section: str = "all") -> dict[str, Any]:
        """Returns the size of the library in GB

        section values: all, movies, tv."""
        return await plex_library_size(services, section)

    async def plex_overview_tool(
        section: str = "all",
        include_activity: bool = True,
        include_recently_added: bool = True,
        include_staleness: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """section values: all, movies, tv."""
        return await plex_overview(
            services, section, include_activity, include_recently_added, include_staleness, limit
        )

    async def media_search_tool(
        query: str | None = None,
        types: list[str] | None = None,
        include_existing: bool = True,
        include_external_lookup: bool = True,
        limit: int = 10,
        director: str | None = None,
        actor: str | None = None,
        genre: str | None = None,
        language: str | None = None,
        year: int | None = None,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Search for movies, TV series, or Plex items ONLY. Returns tmdb_id/tvdb_id for use
        with add tools. For music (albums, sample packs, artists) do NOT use this tool at all,
        including the genre filter — there is no music catalog here. Use nasdoom_find(scope='music')
        instead.

        Either query or at least one attribute filter must be provided. They can be combined.

        types values (list): movie, series, plex. Defaults to all three.
          Narrow types to reduce noise: use ["movie"] for Radarr operations, ["series"] for
          Sonarr operations, ["plex"] for Plex-only queries.

        include_external_lookup: set to false when locating an existing item for delete, research,
          or queue operations — those workflows only need items already in Radarr/Sonarr.
          External lookup is only needed when adding new content not yet in the library.

        Attribute filters:
          director, actor, country — matched via Plex only (Radarr/Sonarr lack these fields).
            When any of these are set, Plex is searched automatically even if not in types.
            Plex requires the full name exactly (e.g. "Akira Kurosawa", not "Kurosawa").
            The existing Radarr/Sonarr library is suppressed when one of these is set (it
            can't filter on crew), but external lookup still runs on the query — those
            results are NOT crew-filtered, and a warning says so. Combine a title query
            with a crew filter to keep getting addable candidates.
          year — matched via every source, including external lookup (use it to pin down
            the right release when adding, e.g. query="Air Force One", year=1997).
          genre, language — matched via the existing Radarr/Sonarr library and Plex, but
            NOT applied to external lookup results.
            language matches originalLanguage in Radarr and audioLanguage in Plex (e.g. "Japanese").
            genre is a substring match (e.g. "Drama" matches "Drama", "Drama/Thriller").

        A query always drives external lookup (when include_external_lookup is true);
        attribute filters refine results, they no longer disable it.

        Each candidate includes match_type and safe_for_action. Act automatically on candidates only
        where safe_for_action is true (exact title match, plus year match if a year was supplied in
        the query). For everything else, present candidates to the user and ask for disambiguation
        before any destructive call.
        """
        return await media_search(
            services,
            query,
            types,
            include_existing,
            include_external_lookup,
            limit,
            director=director,
            actor=actor,
            genre=genre,
            language=language,
            year=year,
            country=country,
        )

    async def radarr_add_movie_tool(
        tmdb_id: int,
        title: str | None = None,
        year: int | None = None,
        quality_profile_id: int | None = None,
        root_folder: str | None = None,
        minimum_availability: str | None = None,
        monitored: bool = True,
        search_now: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add a movie to Radarr.

        tmdb_id must come from a media_search candidate — never recall or construct one.
        Run media_search first and take tmdb_id from a result.

        confirm: false (default) is a dry run — it returns a would_add preview and performs
          no upstream call. Check would_add.title and would_add.year match the intended movie,
          then call again with confirm=true to actually add it.

        minimum_availability values: announced, inCinemas, released, tba.
        """
        return await radarr_add_movie(
            services,
            tmdb_id,
            title,
            year,
            quality_profile_id,
            root_folder,
            minimum_availability,
            monitored,
            search_now,
            confirm,
        )

    async def radarr_research_movie_tool(
        radarr_id: int,
        mode: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Trigger a Radarr action on an existing movie. Use media_search to get radarr_id.

        This is the right tool when a movie is stuck, missing a file, has the wrong quality,
        or needs a re-grab — use it before considering delete/re-add.

        mode values:
          search         — ask Radarr to search indexers for a new or better release.
                           Use when the movie has no file, is the wrong quality, or a re-grab is needed.
          refresh        — reload metadata from TMDb without triggering a new download search.
          scan_downloaded — rescan the movie's folder to import a file already on disk.
                           Use when a file exists but Radarr hasn't picked it up yet.
        """
        return await radarr_research_movie(services, radarr_id, mode, confirm)

    async def sonarr_add_series_tool(
        tvdb_id: int,
        title: str | None = None,
        quality_profile_id: int | None = None,
        root_folder: str | None = None,
        series_type: str | None = None,
        season_folder: bool = True,
        monitor: str = "future",
        search_now: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add a TV series to Sonarr.

        tvdb_id must come from a media_search candidate — never recall or construct one.
        Run media_search first and take tvdb_id from a result.

        confirm: false (default) is a dry run — it returns a would_add preview and performs
          no upstream call. Check the preview title/year match the intended series, then call
          again with confirm=true to actually add it.

        monitor values: all, future, missing, existing, pilot, firstSeason, latestSeason, none.
        Use firstSeason to monitor only S1, latestSeason for the newest season only.
        """
        return await sonarr_add_series(
            services,
            tvdb_id,
            title,
            quality_profile_id,
            root_folder,
            series_type,
            season_folder,
            monitor,
            search_now,
            confirm,
        )

    async def sonarr_research_series_tool(
        sonarr_id: int,
        mode: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Trigger a Sonarr action on an existing series. Use media_search to get sonarr_id.

        This is the right tool when episodes are missing, stuck, or need a re-grab —
        use it before considering delete/re-add.

        mode values:
          series_search         — search indexers for all monitored episodes in the series.
          missing_episode_search — search only for episodes Sonarr has flagged as missing.
                                   Prefer this over series_search when only a subset are missing.
          refresh               — reload metadata from TVDb without triggering a download search.
        """
        return await sonarr_research_series(services, sonarr_id, mode, confirm)

    async def radarr_queue_action_tool(
        queue_id: int,
        action: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Act on a stuck Radarr queue item. Obtain queue_id from radarr_overview.

        action values:
          remove    — clears the item from the queue without blacklisting the release;
                      Radarr may re-grab the same release on the next search.
          blocklist — clears the item and marks the release as unwanted so it won't be re-grabbed.
        """
        return await radarr_queue_action(services, queue_id, action, confirm)

    async def sonarr_queue_action_tool(
        queue_id: int,
        action: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Act on a stuck Sonarr queue item. Obtain queue_id from sonarr_overview.

        action values:
          remove    — clears the item from the queue without blacklisting the release;
                      Sonarr may re-grab the same release on the next search.
          blocklist — clears the item and marks the release as unwanted so it won't be re-grabbed.
        """
        return await sonarr_queue_action(services, queue_id, action, confirm)

    async def radarr_delete_movie_tool(
        radarr_id: int,
        delete_files: bool = True,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove a single movie from Radarr. For multiple, use radarr_delete_movies_batch.

        Identify radarr_id via media_search with include_external_lookup=false and act only on
        candidates where safe_for_action is true.

        delete_files: false removes the movie from Radarr management but leaves the
          file on disk. Typically file itself should be deleted on a delete request.
        add_import_exclusion: prevents Radarr from re-importing or re-monitoring this movie
          after a future library scan. Set to true when you do not want it re-added automatically.
        confirm: false (default) is a strict dry run — returns a preview including size_on_disk_gb
          and performs no upstream call. Set to true to execute the deletion.
        """
        return await radarr_delete_movie(services, radarr_id, delete_files, add_import_exclusion, confirm)

    async def radarr_delete_movies_batch_tool(
        radarr_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove multiple movies from Radarr in one call.

        Each id must be the Radarr internal id (not tmdb_id). Resolve ids via media_search with
        include_external_lookup=false and act only on candidates where safe_for_action is true.

        confirm=false returns a dry-run preview: per-item rows under would_delete, any unknown ids
        under not_found, and a summary including estimated_size_gb.
        confirm=true executes deletions sequentially. Failures do not stop the run; each is collected
        under failed[] alongside its error_type, and the summary reports total_size_deleted_gb for
        successful items only.

        delete_files / add_import_exclusion apply to every selected item — see radarr_delete_movie
        for semantics.
        """
        return await radarr_delete_movies_batch(services, radarr_ids, delete_files, add_import_exclusion, confirm)

    async def sonarr_delete_series_tool(
        sonarr_id: int,
        delete_files: bool = True,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove a single series from Sonarr. For multiple, use sonarr_delete_series_batch.

        Identify sonarr_id via media_search with include_external_lookup=false and act only on
        candidates where safe_for_action is true.

        delete_files: false removes the series from Sonarr management but leaves files on disk.
          Typically the files themselves should be deleted on a delete request.
        add_import_exclusion: prevents Sonarr from re-importing or re-monitoring this series
          after a future library scan. Set to true when you do not want it re-added automatically.
        confirm: false (default) is a strict dry run — returns a preview including size_on_disk_gb
          and performs no upstream call. Set to true to execute the deletion.
        """
        return await sonarr_delete_series(services, sonarr_id, delete_files, add_import_exclusion, confirm)

    async def sonarr_delete_series_batch_tool(
        sonarr_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove multiple series from Sonarr in one call.

        Each id must be the Sonarr internal id (not tvdb_id). Resolve ids via media_search with
        include_external_lookup=false and act only on candidates where safe_for_action is true.

        confirm=false returns a dry-run preview: per-item rows under would_delete, any unknown ids
        under not_found, and a summary including estimated_size_gb.
        confirm=true executes deletions sequentially. Failures do not stop the run; each is collected
        under failed[] alongside its error_type, and the summary reports total_size_deleted_gb for
        successful items only.

        delete_files / add_import_exclusion apply to every selected item — see sonarr_delete_series
        for semantics.
        """
        return await sonarr_delete_series_batch(services, sonarr_ids, delete_files, add_import_exclusion, confirm)

    async def staleness_report_tool(
        media_type: str = "all",
        older_than_days: int = 120,
        include_unwatched: bool = True,
        include_unmanaged: bool = False,
        include_missing: bool = False,
        limit: int = 100,
        sort: str = "staleness_desc",
    ) -> dict[str, Any]:
        """Lists items that have not been watched for a while.

        media_type values: all, movies, tv.

        Buckets (when include_unwatched=true):
          added_long_ago_unwatched — view_count is zero AND last_viewed_at is null AND
                                     added_at is older than older_than_days.
          watched_long_ago         — last_viewed_at is older than older_than_days
                                     (regardless of when the item was added).

        Each item in those buckets includes radarr_id or sonarr_id (joined by exact
        title+year against the Radarr/Sonarr libraries) and match_status. Items with
        match_status="unmanaged" can be deleted via Plex only, not Radarr/Sonarr.

        sort values:
          staleness_desc (default) — oldest most-recent-activity first (sorted by
            max(added_at, last_viewed_at) ascending). Items with neither timestamp sort last.
          size_desc — largest size_on_disk_gb first, nulls last.
          title_asc — alphabetical.
        limit is applied after sort.
        """
        return await staleness_report(
            services,
            media_type,
            older_than_days,
            include_unwatched,
            include_unmanaged,
            include_missing,
            limit,
            sort,
        )

    async def tautulli_history_tool(
        user: str | None = None,
        rating_key: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        media_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Returns Tautulli watch history.

        media_type values: movie, episode.
        start_date / end_date format: YYYY-MM-DD.
        rating_key: Plex rating key to filter history for a specific item.
        """
        return await tautulli_history(services, user, rating_key, start_date, end_date, media_type, limit)

    async def tautulli_users_tool() -> dict[str, Any]:
        return await tautulli_users(services)

    async def tautulli_user_stats_tool(
        user_id: int | None = None,
        grouping: str = "monthly",
    ) -> dict[str, Any]:
        """grouping values: daily, monthly, total."""
        return await tautulli_user_stats(services, user_id, grouping)

    async def nas_backup_health_tool() -> dict[str, Any]:
        """Checks the restic backup jobs (local daily, NAS offsite, SSD emergency)
        via read-only systemd status — does not start/stop/restart anything.

        Each entry reports status: ok, stale, failed, or never_run, plus
        hours_since_last_run. Use this to answer "are my backups actually
        running" instead of assuming a quiet timer means a healthy backup.
        """
        return await nas_backup_health()

    async def nas_storage_inventory_tool(labels: list[str] | None = None) -> dict[str, Any]:
        """Size/entry-count/freshness summary for a fixed allowlist of NAS
        directories outside the Plex media library (music samples, production
        kits, the general transfer/drop folder). Omit `labels` for all of them.
        Read-only: never opens file contents, only stats them.
        """
        return await nas_storage_inventory(labels)

    async def prowlarr_overview_tool() -> dict[str, Any]:
        """Prowlarr indexer health: reachability, per-indexer enabled state,
        which indexers (if any) are disabled/failing.

        Note on the `health` array: an "IndexerVIPExpiredCheck" entry means a
        premium VIP perk (extra API allowance/priority) lapsed on that one
        indexer, NOT that the indexer stopped working — check `enable` on the
        matching indexer before treating it as something the operator needs
        to act on. Only flag it as attention-worthy if the indexer is also
        disabled or actually failing searches.
        """
        return await prowlarr_overview(services)

    async def sabnzbd_overview_tool() -> dict[str, Any]:
        """SABnzbd download-client status: paused state, current speed/ETA,
        queue preview, and recent failed downloads from history."""
        return await sabnzbd_overview(services)

    async def jellyseerr_overview_tool(limit: int = 20) -> dict[str, Any]:
        """Jellyseerr request state: aggregate counts (pending/approved/
        available/etc) plus the most recent pending requests — who asked for
        what and when. This is the "still waiting on a request" answer that
        staleness_report/media_status can't give on their own."""
        return await jellyseerr_overview(services, limit)

    async def nasdoom_health_tool(nocache: bool = True) -> dict[str, Any]:
        """NASDOOM BFF's own service rollup — reachability + latency for all
        8 upstreams (Plex/Sonarr/Radarr/Prowlarr/SABnzbd/nzbget/Tautulli/
        Jellyseerr) in one call. Prefer this over calling media_status +
        prowlarr_overview + sabnzbd_overview + jellyseerr_overview
        separately when you just need a quick "is everything up" check.
        nocache is accepted but has no effect — this call is always live,
        never cached, so there's nothing to bypass."""
        return await nasdoom_health(services)

    async def nasdoom_queue_tool(nocache: bool = True) -> dict[str, Any]:
        """NASDOOM's unified download/import queue — merges SABnzbd and the
        arr import queues into one view with global speed/pause state and
        per-item progress. Prefer this over sabnzbd_overview alone; it also
        covers items already past SAB and sitting in Radarr/Sonarr import.
        nocache is accepted but has no effect — this call is always live."""
        return await nasdoom_queue(services)

    async def nasdoom_omni_search_tool(query: str) -> dict[str, Any]:
        """Cross-source title search (Jellyseerr/Radarr/Sonarr reconciled) —
        answers "do we have X" / "is X available" for a specific title, with
        presence already resolved (inLibrary/managed/acquirable). This is
        the right tool for a one-off title lookup; staleness_report is for
        library-wide sweeps, not single-title questions."""
        return await nasdoom_omni_search(services, query)

    async def nasdoom_requests_overview_tool(filter: str = "pending", take: int = 20) -> dict[str, Any]:
        """NASDOOM's friend-request concierge view — who requested what and
        its human-readable state (needs_approval, awaiting_release,
        downloading, available, etc). filter: all|pending|approved|declined
        |processing|available. Prefer this over jellyseerr_overview for
        anything request-shaped; it has friendlier state labels."""
        return await nasdoom_requests_overview(services, filter, take)

    async def nasdoom_control_tool(nocache: bool = True) -> dict[str, Any]:
        """Operator status: quality-profile/root-folder config for both arrs,
        and storage with a real denominator (percentFull, sourced from arr
        diskspace on the media pool — not a raw `du`). Prefer this over
        nas_storage_inventory for "how full is the media pool"; that tool is
        for the non-media watched dirs (Music/samples/Transfer) only.
        IMPORTANT: this tool alone does NOT answer "how much free space is on
        the NAS overall" — it's media-pool-only. For a whole-NAS/overall
        question, call this AND nas_storage_inventory and combine both;
        don't answer an "overall" question with media-pool-only numbers.
        nocache is accepted but has no effect — this call is always live."""
        return await nasdoom_control(services)

    async def nasdoom_queue_command_tool(
        action: str, value: float | None = None, unit: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Global download-queue control. action: pause|resume|speedcap.
        For speedcap, value is required (0 = unlimited) and unit is
        'percent' or 'mbps'. Reversible — pausing/resuming/re-capping can
        always be undone with another call.

        Two-step: call with confirm=false first (default) — returns the
        current queue state plus what would change, no write happens. Only
        call again with confirm=true once you've decided this is right."""
        return await nasdoom_queue_command(services, action, value, unit, confirm)

    async def nasdoom_queue_item_command_tool(
        item_id: str, action: str, value: float | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Per-item download-queue control. Get item_id from nasdoom_queue.
        action: pause|resume|cancel|priority (value=new priority, item type
        dependent). SABnzbd items take all four; arr-import items only take
        cancel (others 422 unsupported_on_import_lane — that's expected, not
        a bug, explain it plainly if it happens).

        Two-step: confirm=false (default) returns the matched item's current
        state and what would change, no write happens. confirm=true executes."""
        return await nasdoom_queue_item_command(services, item_id, action, value, confirm)

    async def nasdoom_requests_action_tool(request_id: str, action: str, confirm: bool = False) -> dict[str, Any]:
        """Approve or decline a friend's Jellyseerr request. Get request_id
        from nasdoom_requests_overview. action: approve|decline. A status
        change, not destructive — declining doesn't delete anything and can
        be reversed by approving later if the requester is asked again.

        Two-step: confirm=false (default) looks up the actual request (title,
        requester) so you can verify the ID is right before acting — request_
        found=false means the ID doesn't match anything, don't proceed.
        confirm=true executes."""
        return await nasdoom_requests_action(services, request_id, action, confirm)

    async def nasdoom_match_search_tool(rating_key: str, query: str | None = None) -> dict[str, Any]:
        """Find Plex match candidates for a mismatched library item (what
        Plex's own "Fix Match" offers). rating_key must be numeric — get it
        from nasdoom_omni_search's plex.ratingKey or a staleness_report item.
        Optional query does a typed title search for the candidate list."""
        return await nasdoom_match_search(services, rating_key, query)

    async def nasdoom_match_apply_tool(
        rating_key: str, guid: str, name: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Apply a chosen match from nasdoom_match_search — Plex re-matches
        the item and refreshes its metadata. guid/name come from the
        candidate you're applying, not free text.

        Two-step: confirm=false (default) echoes back what would be applied,
        no write happens. confirm=true executes."""
        return await nasdoom_match_apply(services, rating_key, guid, name, confirm)

    async def nasdoom_find_tool(query: str, scope: str = "music") -> dict[str, Any]:
        """Search for non-video content (music samples/kits, software, games,
        books) via Prowlarr — the right tool for "find me a sample pack for
        X" or similar. scope: music|software|games|books. NOT for movies/TV
        — use nasdoom_omni_search for those. Returns a list of grabId values
        (opaque, single-use, expire in 30 min) — pass one to nasdoom_find_grab
        to actually download it. Sorted by popularity (grabs count)."""
        return await nasdoom_find(services, query, scope)

    async def nasdoom_find_grab_tool(grab_id: str, share: bool = False, confirm: bool = False) -> dict[str, Any]:
        """Download a result from nasdoom_find. grab_id comes from that
        call's results — use one within ~30 minutes or it expires (search
        again if so). share=true routes it into the shared/Transfer portal
        folder (visible on the friend-facing share site) instead of your
        private library — only set this if that's actually what was asked
        for, default is private.

        Two-step: confirm=false (default) echoes back what would be grabbed,
        no download happens. confirm=true executes — this spends bandwidth
        and disk space for real, so don't skip the preview."""
        return await nasdoom_find_grab(services, grab_id, share, confirm)

    async def nasdoom_share_friends_list_tool() -> dict[str, Any]:
        """List friend accounts on the file-share portal (files.logueos.xyz)
        — who has access, not what they've uploaded (see
        nasdoom_share_files_list for that)."""
        return await nasdoom_share_friends_list(services)

    async def nasdoom_share_files_list_tool() -> dict[str, Any]:
        """List what's currently in the shared /Transfer folder — name,
        size, whether it's a directory, last modified."""
        return await nasdoom_share_files_list(services)

    async def nasdoom_share_friend_create_tool(
        name: str, upload: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Create a friend account for the file-share portal. Download-only
        by default (upload=false) — set upload=true to let them drop new
        files into /Transfer too (they still can't overwrite/delete
        anything, browse elsewhere, rename, or share, regardless of upload).
        Returns {username, password} on success — that's a real login you
        need to hand to the person, not something to lose track of.

        Two-step: confirm=false (default) echoes back the name/upload
        setting, no account is created. confirm=true creates it for real —
        this hands out real access, so make sure the name is right."""
        return await nasdoom_share_friend_create(services, name, upload, confirm)

    async def nasdoom_share_friend_revoke_tool(friend_id: str, confirm: bool = False) -> dict[str, Any]:
        """Revoke a friend's access to the file-share portal. Get friend_id
        from nasdoom_share_friends_list. Reversible in the sense that you
        can recreate the account, but the old password is gone and a new
        one gets generated.

        Two-step: confirm=false (default) looks up the friend by ID so you
        can verify it's the right person (friend_found=false means the ID
        doesn't match anyone — don't proceed). confirm=true revokes."""
        return await nasdoom_share_friend_revoke(services, friend_id, confirm)

    async def escalate_to_worker_tool(
        issue: str, worker: str = DEFAULT_ESCALATION_WORKER, target_repo: str = DEFAULT_TARGET_REPO
    ) -> dict[str, Any]:
        """Hand off an issue you found but can't fix with your own tools to
        a full LogueOS worker (default: claude-code — full shell/SSH access
        and system-level judgment, not scoped to just this MCP server's
        tools). Use this for anything requiring actually touching system
        config, restarting services, or investigation beyond a single API
        call — e.g. a broken backup path, a service that's down, a config
        drift. `issue` should be a clear, complete description: what you
        found, why it matters, and anything you already ruled out — the
        worker starts fresh with no memory of this conversation. Returns a
        trace_id; tell the operator you escalated and why, don't just go
        quiet."""
        return await escalate_to_worker(services, issue, worker, target_repo)

    register_tool(mcp, "media_status", READ_ONLY, media_status_tool)
    register_tool(mcp, "radarr_overview", READ_ONLY, radarr_overview_tool)
    register_tool(mcp, "sonarr_overview", READ_ONLY, sonarr_overview_tool)
    register_tool(mcp, "plex_overview", READ_ONLY, plex_overview_tool)
    register_tool(mcp, "plex_library_size", READ_ONLY, plex_library_size_tool)
    register_tool(mcp, "media_search", READ_ONLY, media_search_tool)
    register_tool(mcp, "radarr_add_movie", WRITE, radarr_add_movie_tool)
    register_tool(mcp, "radarr_research_movie", WRITE, radarr_research_movie_tool)
    register_tool(mcp, "sonarr_add_series", WRITE, sonarr_add_series_tool)
    register_tool(mcp, "sonarr_research_series", WRITE, sonarr_research_series_tool)
    register_tool(mcp, "radarr_delete_movie", DESTRUCTIVE, radarr_delete_movie_tool)
    register_tool(mcp, "radarr_delete_movies_batch", DESTRUCTIVE, radarr_delete_movies_batch_tool)
    register_tool(mcp, "sonarr_delete_series", DESTRUCTIVE, sonarr_delete_series_tool)
    register_tool(mcp, "sonarr_delete_series_batch", DESTRUCTIVE, sonarr_delete_series_batch_tool)
    register_tool(mcp, "radarr_queue_action", WRITE, radarr_queue_action_tool)
    register_tool(mcp, "sonarr_queue_action", WRITE, sonarr_queue_action_tool)
    register_tool(mcp, "staleness_report", READ_ONLY, staleness_report_tool)
    register_tool(mcp, "tautulli_history", READ_ONLY, tautulli_history_tool)
    register_tool(mcp, "tautulli_users", READ_ONLY, tautulli_users_tool)
    register_tool(mcp, "tautulli_user_stats", READ_ONLY, tautulli_user_stats_tool)
    register_tool(mcp, "nas_backup_health", READ_ONLY, nas_backup_health_tool)
    register_tool(mcp, "nas_storage_inventory", READ_ONLY, nas_storage_inventory_tool)
    register_tool(mcp, "prowlarr_overview", READ_ONLY, prowlarr_overview_tool)
    register_tool(mcp, "sabnzbd_overview", READ_ONLY, sabnzbd_overview_tool)
    register_tool(mcp, "jellyseerr_overview", READ_ONLY, jellyseerr_overview_tool)
    register_tool(mcp, "nasdoom_health", READ_ONLY, nasdoom_health_tool)
    register_tool(mcp, "nasdoom_queue", READ_ONLY, nasdoom_queue_tool)
    register_tool(mcp, "nasdoom_omni_search", READ_ONLY, nasdoom_omni_search_tool)
    register_tool(mcp, "nasdoom_requests_overview", READ_ONLY, nasdoom_requests_overview_tool)
    register_tool(mcp, "nasdoom_control", READ_ONLY, nasdoom_control_tool)
    register_tool(mcp, "nasdoom_queue_command", WRITE, nasdoom_queue_command_tool)
    register_tool(mcp, "nasdoom_queue_item_command", WRITE, nasdoom_queue_item_command_tool)
    register_tool(mcp, "nasdoom_requests_action", WRITE, nasdoom_requests_action_tool)
    register_tool(mcp, "nasdoom_match_search", READ_ONLY, nasdoom_match_search_tool)
    register_tool(mcp, "nasdoom_match_apply", WRITE, nasdoom_match_apply_tool)
    register_tool(mcp, "nasdoom_find", READ_ONLY, nasdoom_find_tool)
    register_tool(mcp, "nasdoom_find_grab", WRITE, nasdoom_find_grab_tool)
    register_tool(mcp, "nasdoom_share_friends_list", READ_ONLY, nasdoom_share_friends_list_tool)
    register_tool(mcp, "nasdoom_share_files_list", READ_ONLY, nasdoom_share_files_list_tool)
    register_tool(mcp, "nasdoom_share_friend_create", WRITE, nasdoom_share_friend_create_tool)
    register_tool(mcp, "nasdoom_share_friend_revoke", WRITE, nasdoom_share_friend_revoke_tool)
    register_tool(mcp, "escalate_to_worker", WRITE, escalate_to_worker_tool)
    return mcp


def register_tool(
    mcp: FastMCP,
    name: str,
    annotations: dict[str, bool],
    func: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    func.__name__ = name
    try:
        decorator = mcp.tool(name=name, annotations=annotations)
    except TypeError:
        decorator = mcp.tool(name=name)
    decorator(func)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or load_settings()
    services = build_services(settings)
    mcp = create_mcp(services)
    try:
        mcp_app = mcp.http_app(path="/mcp")
    except TypeError:
        mcp_app = mcp.http_app()

    oauth_store = OAuthStore(
        bearer_token=settings.mcp_bearer_token.get_secret_value(),
        base_url=str(settings.mcp_public_base_url).rstrip("/") if settings.mcp_public_base_url else "",
        access_token_ttl=settings.oauth_access_token_ttl,
        state_path=settings.oauth_state_path,
    )

    starlette_app = Starlette(
        routes=[
            Route("/.well-known/oauth-authorization-server", oauth_store.handle_discovery),
            Route("/oauth/authorize", oauth_store.handle_authorize_get, methods=["GET"]),
            Route("/oauth/authorize", oauth_store.handle_authorize_post, methods=["POST"]),
            Route("/oauth/token", oauth_store.handle_token, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )

    return BearerAuthApp(starlette_app, settings.mcp_bearer_token.get_secret_value(), oauth_store)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    logger.info(
        "Starting Seedbox MCP with config: %s",
        json.dumps(settings.redacted_summary(), sort_keys=True),
    )
    uvicorn.run(create_app(settings), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
