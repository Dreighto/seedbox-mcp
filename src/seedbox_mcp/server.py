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
from seedbox_mcp.import_diagnosis import nas_import_diagnosis
from seedbox_mcp.oauth import OAuthStore
from seedbox_mcp.runtime import Services, build_services
from seedbox_mcp.tools.adguard import adguard_protection, adguard_stats
from seedbox_mcp.tools.downloads import (
    jellyseerr_overview,
    prowlarr_indexer_stats,
    prowlarr_overview,
    sabnzbd_overview,
)
from seedbox_mcp.tools.escalate import DEFAULT_ESCALATION_WORKER, DEFAULT_TARGET_REPO, escalate_to_worker
from seedbox_mcp.tools.fleet import fleet_health
from seedbox_mcp.tools.gotify import gotify_alerts
from seedbox_mcp.tools.host_health import (
    nas_disk_health,
    nas_log_search,
    nas_resources,
    nas_service_restart,
    nas_service_status,
)
from seedbox_mcp.tools.jellyseerr import jellyseerr_request_add, jellyseerr_search
from seedbox_mcp.tools.nas_network import nas_internet_speed_test
from seedbox_mcp.tools.nas_storage import nas_backup_health, nas_storage_inventory
from seedbox_mcp.tools.nasdoom import (
    nasdoom_add,
    nasdoom_control,
    nasdoom_find,
    nasdoom_find_grab,
    nasdoom_fix_import,
    nasdoom_grab_release,
    nasdoom_health,
    nasdoom_match_apply,
    nasdoom_match_search,
    nasdoom_omni_search,
    nasdoom_profiles,
    nasdoom_queue,
    nasdoom_queue_command,
    nasdoom_queue_item_command,
    nasdoom_releases,
    nasdoom_requests_action,
    nasdoom_requests_overview,
    nasdoom_share_files_list,
    nasdoom_share_friend_create,
    nasdoom_share_friend_revoke,
    nasdoom_share_friends_list,
)
from seedbox_mcp.tools.plex import plex_library_size, plex_now_playing, plex_overview
from seedbox_mcp.tools.poster_ocr import poster_ocr
from seedbox_mcp.tools.radarr import (
    radarr_add_movie,
    radarr_blocklist,
    radarr_blocklist_remove,
    radarr_calendar,
    radarr_delete_movie,
    radarr_delete_movies_batch,
    radarr_overview,
    radarr_queue_action,
    radarr_research_movie,
)
from seedbox_mcp.tools.search import media_search
from seedbox_mcp.tools.sonarr import (
    sonarr_add_series,
    sonarr_blocklist,
    sonarr_blocklist_remove,
    sonarr_calendar,
    sonarr_delete_series,
    sonarr_delete_series_batch,
    sonarr_monitor_season,
    sonarr_overview,
    sonarr_queue_action,
    sonarr_research_series,
)
from seedbox_mcp.tools.staleness import staleness_report
from seedbox_mcp.tools.status import media_status
from seedbox_mcp.tools.tautulli import (
    tautulli_history,
    tautulli_user_stats,
    tautulli_users,
)
from seedbox_mcp.tools.tdarr import tdarr_status
from seedbox_mcp.tools.web_search import content_release_status, web_fetch, web_search

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
        include_seasons: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Returns Sonarr library state.

        Use include_queue=true to retrieve queue_id values needed by sonarr_queue_action.
        Each queue item also carries sonarr_id (for sonarr_research_series) plus a clean
        title and the raw release_title — act on these directly rather than feeding the
        release name back through media_search.
        Set include_series=false or include_missing=false to reduce response size when
        only queue data is needed.

        Set include_seasons=true to get a per-season breakdown for each series: which
        seasons exist, whether each is monitored, and how many episodes are on disk. This
        is how to answer "what seasons of <show> do we have". Pair with media_search to
        find the show's sonarr_id, then match it in this list. Feed a season_number into
        sonarr_monitor_season to start collecting a season that isn't on disk yet.
        """
        return await sonarr_overview(services, include_series, include_queue, include_missing, include_seasons, limit)

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

    async def sonarr_monitor_season_tool(
        sonarr_id: int,
        season_number: int,
        search_now: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Start collecting one season of a series already in Sonarr, then search for it.

        Use this for "add season N of <show>" when the show already exists in Sonarr but
        that season isn't on disk. Get sonarr_id from media_search, and confirm the season
        is missing via sonarr_overview(include_seasons=true). If the show is NOT in Sonarr
        yet, call sonarr_add_series first (monitor=none), then this.

        It marks the season monitored (and the series, which Sonarr requires) and, when
        search_now=true, triggers an indexer search for that season's episodes.

        confirm: false (default) is a dry run returning a would_monitor preview with no
          upstream change. Show it, then call again with confirm=true once the user agrees.
        """
        return await sonarr_monitor_season(services, sonarr_id, season_number, search_now, confirm)

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

    async def radarr_calendar_tool(days_ahead: int = 14) -> dict[str, Any]:
        """Upcoming movie releases (physical/digital) for the next N days
        (default 14, max 90) — the right tool for "what's coming out soon" /
        "when does X release". Only covers titles already tracked in Radarr."""
        return await radarr_calendar(services, days_ahead)

    async def sonarr_calendar_tool(days_ahead: int = 14) -> dict[str, Any]:
        """Upcoming episode air dates for the next N days (default 14, max 90)
        — the right tool for "what's airing this week" / "when's the next
        episode of X". Only covers series already tracked in Sonarr."""
        return await sonarr_calendar(services, days_ahead)

    async def radarr_blocklist_tool(limit: int = 20) -> dict[str, Any]:
        """Recently blocklisted Radarr releases (failed/rejected grabs Radarr
        won't retry). Use to explain why a movie isn't downloading, or to find
        a blocklist_id for radarr_blocklist_remove."""
        return await radarr_blocklist(services, limit)

    async def sonarr_blocklist_tool(limit: int = 20) -> dict[str, Any]:
        """Recently blocklisted Sonarr releases (failed/rejected grabs Sonarr
        won't retry). Use to explain why an episode isn't downloading, or to
        find a blocklist_id for sonarr_blocklist_remove."""
        return await sonarr_blocklist(services, limit)

    async def radarr_blocklist_remove_tool(blocklist_id: int, confirm: bool = False) -> dict[str, Any]:
        """Un-blocklist a Radarr release so it becomes eligible to be grabbed
        again. Get blocklist_id from radarr_blocklist. Reversible — the
        release can always be re-blocklisted via radarr_queue_action if
        it turns out to be bad again.

        Two-step: confirm=false (default) previews, confirm=true executes."""
        return await radarr_blocklist_remove(services, blocklist_id, confirm)

    async def sonarr_blocklist_remove_tool(blocklist_id: int, confirm: bool = False) -> dict[str, Any]:
        """Un-blocklist a Sonarr release so it becomes eligible to be grabbed
        again. Get blocklist_id from sonarr_blocklist. Reversible — the
        release can always be re-blocklisted via sonarr_queue_action if
        it turns out to be bad again.

        Two-step: confirm=false (default) previews, confirm=true executes."""
        return await sonarr_blocklist_remove(services, blocklist_id, confirm)

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
        """The real Plex user roster — usernames, friendly names, emails,
        active flag. THIS is the tool for "how many users do I have",
        "what are their names", "who has access". Report the real names
        returned; never invent a count or names, and never answer this
        from a request-count overview."""
        return await tautulli_users(services)

    async def plex_now_playing_tool() -> dict[str, Any]:
        """Who is watching RIGHT NOW — live Plex streams read straight from
        Plex's own /status/sessions (authoritative; do NOT rely on Tautulli
        for this). Returns stream_count, and per stream: user, title,
        local (LAN) vs remote (WAN), direct-play vs transcode, per-stream
        bandwidth, source resolution, and transcode throttle/speed. Also
        returns bottleneck_flags computed in code (concurrent transcodes,
        throttled transcodes, remote streams, high total bandwidth) — the
        answer to "who's watching and is the server struggling". This is
        the real answer to "how many people are watching" / "who's
        streaming"; never answer that from request counts
        (jellyseerr_overview/nasdoom_requests_overview), which count
        requested titles, not viewers. An empty now_playing list means
        nobody is streaming."""
        return await plex_now_playing(services)

    async def tautulli_user_stats_tool(
        user_id: int | None = None,
        grouping: str = "monthly",
    ) -> dict[str, Any]:
        """grouping values: daily, monthly, total."""
        return await tautulli_user_stats(services, user_id, grouping)

    async def nas_backup_health_tool(nocache: bool = True, limit: int | None = None) -> dict[str, Any]:
        """Checks the restic backup jobs (local daily, NAS offsite, SSD emergency)
        via read-only systemd status — does not start/stop/restart anything.

        Each entry reports status: ok, stale, failed, or never_run, plus
        hours_since_last_run. Use this to answer "are my backups actually
        running" instead of assuming a quiet timer means a healthy backup.
        nocache/limit are accepted but have no effect — there are always
        exactly 3 backup jobs, always checked live."""
        return await nas_backup_health()

    async def nas_storage_inventory_tool(labels: list[str] | None = None) -> dict[str, Any]:
        """Size/entry-count/freshness summary for a fixed allowlist of NAS
        directories outside the Plex media library (music samples, production
        kits, the general transfer/drop folder). Omit `labels` for all of them.
        Read-only: never opens file contents, only stats them.
        """
        return await nas_storage_inventory(labels)

    async def nas_internet_speed_test_tool() -> dict[str, Any]:
        """Runs a real speed test from the NAS itself (SSH + speedtest-cli) —
        this is the NAS's own internet connection, NOT the machine this MCP
        server runs on. Takes ~5-10s; use this when asked "what are my NAS's
        internet speeds" or similar — there is no cached/estimated substitute,
        every call runs a fresh real test and consumes real bandwidth (a few
        hundred MB), so don't call it repeatedly in one conversation."""
        return await nas_internet_speed_test()

    async def tdarr_status_tool() -> dict[str, Any]:
        """HEVC transcoding pipeline status from Tdarr: each node (NAS internal
        + the ROOM RTX 5060 Ti GPU node) with paused/worker state, whether
        anything is actively processing, lifetime transcode/health-check
        counts, and space reclaimed (Tdarr-processed files only). USE THIS for
        "how's the transcoding", "is the GPU node up", "how much has Tdarr
        saved". Read-only."""
        return await tdarr_status(services)

    async def gotify_alerts_tool(limit: int = 10) -> dict[str, Any]:
        """Recent alert HISTORY from the Gotify inbox: what has actually fired
        lately (Uptime Kuma node/service up/down events, and anything else
        pushed there). USE THIS for "what alerts fired recently", "what went
        wrong today", "has anything been flapping" — the temporal view that
        fleet_health (a right-now snapshot) doesn't give. `limit` = how many
        most-recent alerts. Read-only."""
        return await gotify_alerts(services, limit)

    async def nas_resources_tool() -> dict[str, Any]:
        """Live NAS resource pressure: CPU load (1/5/15m + per-core), memory
        used/available, swap, uptime, and the top CPU/memory-consuming
        processes, with deterministic pressure flags (high_load,
        memory_pressure, heavy_swap). USE THIS for "is the NAS under load / low
        on memory / running hot / what's hogging it" — the live view that disk
        SMART and free-space checks don't cover. Read-only."""
        return await nas_resources(services)

    async def adguard_stats_tool() -> dict[str, Any]:
        """Ad/tracker-blocking health from AdGuard Home: protection on/off,
        total DNS queries, how many blocked and the block rate, average
        processing time, top blocked domains, and busiest client devices. USE
        THIS for "how's the ad-blocking / network filtering doing" or "is
        AdGuard blocking". Read-only."""
        return await adguard_stats(services)

    async def adguard_protection_tool(
        action: str, minutes: int | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Pause or resume AdGuard network filtering for the WHOLE LAN.
        action='pause' disables ad/tracker blocking for a bounded number of
        minutes (default 10, max 60) and AdGuard auto-re-enables it on its own
        timer, so it can't be left off by accident; action='resume' turns it
        back on now. Two-step: confirm=false previews current state + what
        changes, confirm=true applies. Report the returned
        protection_enabled_now. Use for "turn off ad-blocking for a bit, a
        site is broken" and to turn it back on."""
        return await adguard_protection(services, action, minutes, confirm)

    async def fleet_health_tool() -> dict[str, Any]:
        """Whole-cluster health in one call: up/down for every cluster node
        (NAS, ROOM, apple-node, Jetson, ailogueos) AND every service,
        including the non-media ones (AdGuard, Vaultwarden, monitoring) that
        nasdoom_health doesn't cover. USE THIS to answer "is everything up
        across the NAS/cluster" or to catch a node/non-media outage.
        nasdoom_health stays the right call for a media-stack-only rollup."""
        return await fleet_health(services)

    async def content_release_status_tool(query: str) -> dict[str, Any]:
        """Answer whether a movie/show/anime is out yet, streaming yet, or
        has a new season/batch out — via Perplexity's web-grounded search,
        which returns a synthesized, cited answer (release dates, current
        streaming platform, physical/digital availability) instead of raw
        search results. USE THIS for any "is X out / streaming / released
        yet", "when does X come out", "is season N of X out" question — it's
        more accurate and far leaner than web_search for release timing.
        Use web_search (not this) for general research. Phrase `query` as a
        direct natural-language question."""
        return await content_release_status(services, query)

    async def web_search_tool(query: str, max_results: int = 5) -> dict[str, Any]:
        """Search the live web (Ollama's hosted search API) — use this for
        anything needing current/outside information the NAS's own tools
        don't have: researching a new tool/integration, checking current
        best practices, looking up an error message, or any question whose
        answer isn't "the state of this NAS right now". Returns up to
        max_results (default 5, max 10) results with title/url/snippet.
        Pair with web_fetch to read one result's full page if the snippet
        isn't enough. Not needed for anything about this NAS's own current
        state — use the NAS-specific tools for that, they're faster and
        authoritative."""
        return await web_search(services, query, max_results)

    async def web_fetch_tool(url: str) -> dict[str, Any]:
        """Fetch and read one specific web page's content (title + text,
        truncated to ~8000 chars). Use after web_search when a snippet isn't
        enough detail, or when the operator gives you a URL directly."""
        return await web_fetch(services, url)

    async def poster_ocr_tool(image_b64: str) -> dict[str, Any]:
        """Runs OCR (apple-node's Vision-framework service) on a base64-encoded
        image and returns extracted text lines ranked by prominence (largest
        text first — usually the title on a movie/show poster, but verify it
        reads as a plausible title rather than assuming the first line is
        always right). Follow up with media_search/nasdoom_omni_search on the
        extracted title to check library status."""
        return await poster_ocr(services, image_b64)

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

    async def prowlarr_indexer_stats_tool(nocache: bool = True) -> dict[str, Any]:
        """Per-indexer usage/failure counters (queries, grabs, failures,
        response time) — the right tool for "is an indexer actually working"
        or diagnosing why a search/grab keeps failing on one source, as
        opposed to prowlarr_overview's enabled/disabled snapshot. A nonzero
        failed_auth_queries is the strongest signal something's actually
        wrong (expired key/VIP, not just a transient failure) —
        likely_needs_attention flags that plus a >50% query failure rate.
        nocache is accepted but has no effect — this call is always live."""
        return await prowlarr_indexer_stats(services)

    async def sabnzbd_overview_tool() -> dict[str, Any]:
        """SABnzbd download-client status: paused state, current speed/ETA,
        queue preview, and recent failed downloads from history."""
        return await sabnzbd_overview(services)

    async def jellyseerr_overview_tool(limit: int = 20) -> dict[str, Any]:
        """Jellyseerr request state: aggregate counts (pending/approved/
        available/etc) plus the most recent pending requests — who asked for
        what and when. This is the "still waiting on a request" answer that
        staleness_report/media_status can't give on their own. The counts
        are REQUESTED TITLES, not people or active streams: total=12 means
        12 requested titles, movie=11 means 11 movie requests. Never report
        these as "N people watching" or a user count — for the roster of
        users/accounts use tautulli_users."""
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

    async def nasdoom_requests_overview_tool(filter: str = "all", take: int = 20) -> dict[str, Any]:
        """NASDOOM's friend-request concierge view — who requested what and
        its human-readable state (needs_approval, awaiting_release,
        downloading, available, etc). The `counts` are totals across ALL
        states; the `requests` list is only the `filter` subset. Default
        filter is 'all' — do NOT use 'pending' expecting to see requests, it's
        always empty because requests auto-approve. filter: all|pending|
        approved|declined|processing|available. Prefer this over
        jellyseerr_overview for anything request-shaped."""
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

    async def nasdoom_add_tool(
        kind: str,
        tmdb_id: int | None = None,
        tvdb_id: int | None = None,
        quality_profile_id: int | None = None,
        root_folder_path: str | None = None,
        monitored: bool = True,
        search_now: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add a movie or series to Radarr/Sonarr. kind: 'movie'|'tv'. Get
        tmdb_id (movies) or tmdb_id/tvdb_id (tv) from media_search or
        nasdoom_omni_search first — never recall or construct an id yourself.

        Leave quality_profile_id and root_folder_path unset unless the
        operator specifically asked for a particular one — NASDOOM
        automatically routes anime to the anime folder/profile (detected via
        TMDB genre+origin-language) and everything else to the regular
        library's configured default, which is correct far more often than
        any single hardcoded default would be. Only pass these explicitly
        when the operator names a specific quality or location.

        search_now defaults to false — adding monitors the title without
        triggering an immediate download search, matching how the operator's
        own Jellyseerr-driven flow behaves. Only set true if the operator
        asks to grab it now, not just add/track it.

        Two-step: confirm=false (default) previews what would be sent —
        quality_profile_id/root_folder_path showing as omitted in the
        preview means "NASDOOM will pick automatically", not an error.
        confirm=true executes. Returns error=already_managed with the
        existing arrId if it's already in the library — don't re-add."""
        return await nasdoom_add(
            services, kind, tmdb_id, tvdb_id, quality_profile_id, root_folder_path, monitored, search_now, confirm
        )

    async def nasdoom_fix_import_tool(kind: str, tmdb_id: int, confirm: bool = False) -> dict[str, Any]:
        """Fix a download that's import-blocked because its title isn't in
        the library (nas_import_diagnosis reported match_problem / 'Unknown
        Series'). Adds the missing title and re-checks the arr queue so the
        waiting download imports. kind: 'movie'|'tv'; get tmdb_id from a
        search first (never guess it). Two-step: confirm=false previews,
        confirm=true does it. Use ONLY for the not-in-library case, not for a
        title that IS added but mismatched (that needs a manual match)."""
        return await nasdoom_fix_import(services, kind, tmdb_id, confirm)

    async def nas_import_diagnosis_tool() -> dict[str, Any]:
        """Diagnose WHY downloads are stuck importing into Radarr/Sonarr.
        Scans both queues for import-stuck items and, for each, runs the
        real access test as the arr's own uid inside its container to pin
        the root cause: download-side permissions (the most common),
        library-side permissions, or a path-not-found/mount issue.
        Returns a specific diagnosis and, for permission cases, the exact
        chown/chmod remediation command — which is a filesystem change on
        the NAS the bot does NOT run itself; hand it to the operator or
        escalate. Use this when the queue shows import failures, or when
        the operator asks why something won't import / finish."""
        return await nas_import_diagnosis(services)

    async def nas_log_search_tool(service: str, query: str, lines: int = 40) -> dict[str, Any]:
        """Search a media app's own logs on the NAS (radarr/sonarr/prowlarr)
        for a term — the deep-diagnosis step when queue/status signals aren't
        specific enough. Read-only; returns the last matching log lines from
        the newest few log files. Use it to find the arr's own detailed
        reason for a rejected/failed release (search the release name), a
        failed search, or an error. If nas_import_diagnosis returns a vague
        result, search the release name here for the underlying detail."""
        return await nas_log_search(services, service, query, lines)

    async def nas_disk_health_tool() -> dict[str, Any]:
        """Physical disk health for every drive on the NAS via SMART. Each
        disk gets a verdict computed in code from Backblaze failure-rate
        thresholds: 'ok', 'watch', or 'replace_now', with the exact
        attribute values as reasons. Report the verdicts and reasons as
        given — never soften a 'replace_now' or upgrade an 'ok'."""
        return await nas_disk_health(services)

    async def nas_service_status_tool(name: str | None = None) -> dict[str, Any]:
        """State of the media-stack Docker containers on the NAS (plex,
        radarr, sonarr, prowlarr, sabnzbd, jellyseerr, tautulli, kometa,
        recyclarr, filebrowser). Omit name for all; pass one name for
        just that service. Distinct from nasdoom_health, which checks the
        services' APIs — this checks the containers themselves, so use it
        when an API is unreachable to see whether the container is even
        running."""
        return await nas_service_status(services, name)

    async def nas_service_restart_tool(name: str, confirm: bool = False) -> dict[str, Any]:
        """Restart one container on the NAS: the media stack AND the
        beyond-media services (AdGuard, Vaultwarden, Uptime Kuma, Gotify).
        Shared infrastructure (n8n, ollama, cloudflared) is refused with
        reason=shared_infrastructure; escalate those instead.

        Two-step: confirm=false (default) previews the current container
        state without doing anything. confirm=true restarts and then
        re-checks the real container state — report the returned
        state_after/verified_running values, never assume the restart
        worked."""
        return await nas_service_restart(services, name, confirm)

    async def nasdoom_releases_tool(kind: str, tmdb_id: int) -> dict[str, Any]:
        """Read-only: what actual releases exist for a movie/show right now
        and at what quality. kind: 'movie'|'tv'; get tmdb_id from a search
        first. Returns per-release quality, whether it meets the normal
        720p/1080p profile (meets_standard_profile), and whether it's a
        theatrical rip (theatrical_rip: cam/telesync/screener). Top-level
        standard_quality_available and only_theatrical_rips summarize it.
        Use this to answer 'is a good copy out yet' and to warn a requester
        honestly before grabbing a below-standard release. No download
        happens here."""
        return await nasdoom_releases(services, kind, tmdb_id)

    async def nasdoom_grab_release_tool(grab_id: str, confirm: bool = False) -> dict[str, Any]:
        """Grab one SPECIFIC release by its grab_id from nasdoom_releases,
        overriding the normal quality profile. This is only for the case
        where a requester has been told a release is below standard
        (a theatrical rip / not true streaming quality) and has explicitly
        agreed to it anyway. Two-step: confirm=false previews, confirm=true
        grabs. Do NOT use this for a normal request (that's the standard
        request flow, which already respects the quality profile) — only
        for a knowingly-accepted sub-standard grab."""
        return await nasdoom_grab_release(services, grab_id, confirm)

    async def jellyseerr_search_tool(query: str) -> dict[str, Any]:
        """Search Jellyseerr directly for a movie or TV title. Unlike
        nasdoom_omni_search, this filters out anything flagged adult before
        it ever reaches you — use this instead of nasdoom_omni_search
        whenever the caller might be a friend-tier user, not the operator.
        Returns tmdb_id per title — get it from here before calling
        jellyseerr_request_add, never construct or recall an id yourself."""
        return await jellyseerr_search(services, query)

    async def jellyseerr_request_add_tool(
        kind: str,
        tmdb_id: int,
        title: str,
        year: int | None = None,
        bulk: bool = False,
        confirm: bool = True,
    ) -> dict[str, Any]:
        """Request a movie or TV series through Jellyseerr. kind: 'movie'|
        'tv'. Get tmdb_id/title/year from jellyseerr_search first.

        SINGLE-STEP: this creates the request the moment you call it (a TV
        series requests all seasons). There is NO preview/confirm step — only
        call it once the person has actually asked to add the title, not for a
        plain availability check. It returns the real request id and state;
        report that, never a success before the call ran. (The bulk/confirm
        arguments are accepted for backward compatibility and ignored.)"""
        return await jellyseerr_request_add(services, kind, tmdb_id, title, year)

    async def nasdoom_profiles_tool(kind: str) -> dict[str, Any]:
        """List available quality profiles for 'movie' or 'tv', plus the
        recommended default. Use this only if the operator asks what quality
        options exist or wants something other than the default — nasdoom_add
        already picks a sensible default automatically without calling this
        first."""
        return await nasdoom_profiles(services, kind)

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
    register_tool(mcp, "sonarr_monitor_season", WRITE, sonarr_monitor_season_tool)
    register_tool(mcp, "radarr_delete_movie", DESTRUCTIVE, radarr_delete_movie_tool)
    register_tool(mcp, "radarr_delete_movies_batch", DESTRUCTIVE, radarr_delete_movies_batch_tool)
    register_tool(mcp, "sonarr_delete_series", DESTRUCTIVE, sonarr_delete_series_tool)
    register_tool(mcp, "sonarr_delete_series_batch", DESTRUCTIVE, sonarr_delete_series_batch_tool)
    register_tool(mcp, "radarr_queue_action", WRITE, radarr_queue_action_tool)
    register_tool(mcp, "sonarr_queue_action", WRITE, sonarr_queue_action_tool)
    register_tool(mcp, "radarr_calendar", READ_ONLY, radarr_calendar_tool)
    register_tool(mcp, "sonarr_calendar", READ_ONLY, sonarr_calendar_tool)
    register_tool(mcp, "radarr_blocklist", READ_ONLY, radarr_blocklist_tool)
    register_tool(mcp, "sonarr_blocklist", READ_ONLY, sonarr_blocklist_tool)
    register_tool(mcp, "radarr_blocklist_remove", WRITE, radarr_blocklist_remove_tool)
    register_tool(mcp, "sonarr_blocklist_remove", WRITE, sonarr_blocklist_remove_tool)
    register_tool(mcp, "staleness_report", READ_ONLY, staleness_report_tool)
    register_tool(mcp, "tautulli_history", READ_ONLY, tautulli_history_tool)
    register_tool(mcp, "tautulli_users", READ_ONLY, tautulli_users_tool)
    register_tool(mcp, "plex_now_playing", READ_ONLY, plex_now_playing_tool)
    register_tool(mcp, "tautulli_user_stats", READ_ONLY, tautulli_user_stats_tool)
    register_tool(mcp, "nas_backup_health", READ_ONLY, nas_backup_health_tool)
    register_tool(mcp, "nas_storage_inventory", READ_ONLY, nas_storage_inventory_tool)
    register_tool(mcp, "nas_internet_speed_test", READ_ONLY, nas_internet_speed_test_tool)
    register_tool(mcp, "web_search", READ_ONLY, web_search_tool)
    register_tool(mcp, "content_release_status", READ_ONLY, content_release_status_tool)
    register_tool(mcp, "fleet_health", READ_ONLY, fleet_health_tool)
    register_tool(mcp, "adguard_stats", READ_ONLY, adguard_stats_tool)
    register_tool(mcp, "adguard_protection", WRITE, adguard_protection_tool)
    register_tool(mcp, "nas_resources", READ_ONLY, nas_resources_tool)
    register_tool(mcp, "gotify_alerts", READ_ONLY, gotify_alerts_tool)
    register_tool(mcp, "tdarr_status", READ_ONLY, tdarr_status_tool)
    register_tool(mcp, "web_fetch", READ_ONLY, web_fetch_tool)
    register_tool(mcp, "poster_ocr", READ_ONLY, poster_ocr_tool)
    register_tool(mcp, "prowlarr_overview", READ_ONLY, prowlarr_overview_tool)
    register_tool(mcp, "prowlarr_indexer_stats", READ_ONLY, prowlarr_indexer_stats_tool)
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
    register_tool(mcp, "nasdoom_add", WRITE, nasdoom_add_tool)
    register_tool(mcp, "nasdoom_profiles", READ_ONLY, nasdoom_profiles_tool)
    register_tool(mcp, "jellyseerr_search", READ_ONLY, jellyseerr_search_tool)
    register_tool(mcp, "jellyseerr_request_add", WRITE, jellyseerr_request_add_tool)
    register_tool(mcp, "nasdoom_releases", READ_ONLY, nasdoom_releases_tool)
    register_tool(mcp, "nasdoom_grab_release", WRITE, nasdoom_grab_release_tool)
    register_tool(mcp, "nas_import_diagnosis", READ_ONLY, nas_import_diagnosis_tool)
    register_tool(mcp, "nas_log_search", READ_ONLY, nas_log_search_tool)
    register_tool(mcp, "nasdoom_fix_import", WRITE, nasdoom_fix_import_tool)
    register_tool(mcp, "nas_disk_health", READ_ONLY, nas_disk_health_tool)
    register_tool(mcp, "nas_service_status", READ_ONLY, nas_service_status_tool)
    register_tool(mcp, "nas_service_restart", WRITE, nas_service_restart_tool)
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
