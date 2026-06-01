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

from whatbox_media_mcp.config import Settings, load_settings
from whatbox_media_mcp.oauth import OAuthStore
from whatbox_media_mcp.runtime import Services, build_services
from whatbox_media_mcp.tools.plex import plex_library_size, plex_overview
from whatbox_media_mcp.tools.radarr import (
    radarr_add_movie,
    radarr_delete_movie,
    radarr_overview,
    radarr_queue_action,
    radarr_research_movie,
)
from whatbox_media_mcp.tools.search import media_search
from whatbox_media_mcp.tools.sonarr import (
    sonarr_add_series,
    sonarr_delete_series,
    sonarr_overview,
    sonarr_queue_action,
    sonarr_research_series,
)
from whatbox_media_mcp.tools.staleness import staleness_report
from whatbox_media_mcp.tools.status import media_status
from whatbox_media_mcp.tools.tautulli import tautulli_history, tautulli_user_stats, tautulli_users

logger = logging.getLogger("whatbox_media_mcp")

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
    mcp = FastMCP("Whatbox Media Steward")

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
        """Search for movies, TV series, or Plex items. Returns tmdb_id/tvdb_id for use with add tools.

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
            When a crew/country filter is active, Radarr/Sonarr results are suppressed
            entirely to avoid returning unfiltered partial matches.
          genre, language, year — matched via both Radarr/Sonarr and Plex.
            language matches originalLanguage in Radarr and audioLanguage in Plex (e.g. "Japanese").
            genre is a substring match (e.g. "Drama" matches "Drama", "Drama/Thriller").

        Attribute filters suppress external lookup (include_external_lookup is ignored when filters are set).
        """
        return await media_search(
            services, query, types, include_existing, include_external_lookup, limit,
            director=director, actor=actor, genre=genre, language=language, year=year, country=country,
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
        """Add a movie to Radarr. Use media_search first to get the correct tmdb_id.

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
        """Add a TV series to Sonarr. Use media_search first to get the correct tvdb_id.

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
        delete_files: bool = False,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove a movie from Radarr. Use media_search (include_external_lookup=false) to get radarr_id.

        delete_files: false (default) removes the movie from Radarr management but leaves the
          file on disk — safe when the file should remain accessible in Plex.
          Set to true only when the file itself should be deleted.
        add_import_exclusion: prevents Radarr from re-importing or re-monitoring this movie
          after a future library scan. Set to true when you do not want it re-added automatically.
        confirm: false (default) is a dry run — shows what would be removed without acting.
          Set to true to perform the deletion.
        """
        return await radarr_delete_movie(services, radarr_id, delete_files, add_import_exclusion, confirm)

    async def sonarr_delete_series_tool(
        sonarr_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove a series from Sonarr. Use media_search (include_external_lookup=false) to get sonarr_id.

        delete_files: false (default) removes the series from Sonarr management but leaves files
          on disk — safe when they should remain accessible in Plex.
          Set to true only when the files themselves should be deleted.
        add_import_exclusion: prevents Sonarr from re-importing or re-monitoring this series
          after a future library scan. Set to true when you do not want it re-added automatically.
        confirm: false (default) is a dry run — shows what would be removed without acting.
          Set to true to perform the deletion.
        """
        return await sonarr_delete_series(services, sonarr_id, delete_files, add_import_exclusion, confirm)


    async def staleness_report_tool(
        media_type: str = "all",
        older_than_days: int = 120,
        include_unwatched: bool = True,
        include_unmanaged: bool = False,
        include_missing: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Lists items that have not been watched for a while.
        
        media_type values: all, movies, tv."""
        return await staleness_report(
            services,
            media_type,
            older_than_days,
            include_unwatched,
            include_unmanaged,
            include_missing,
            limit,
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
    register_tool(mcp, "sonarr_delete_series", DESTRUCTIVE, sonarr_delete_series_tool)
    register_tool(mcp, "radarr_queue_action", WRITE, radarr_queue_action_tool)
    register_tool(mcp, "sonarr_queue_action", WRITE, sonarr_queue_action_tool)
    register_tool(mcp, "staleness_report", READ_ONLY, staleness_report_tool)
    register_tool(mcp, "tautulli_history", READ_ONLY, tautulli_history_tool)
    register_tool(mcp, "tautulli_users", READ_ONLY, tautulli_users_tool)
    register_tool(mcp, "tautulli_user_stats", READ_ONLY, tautulli_user_stats_tool)
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
        "Starting Whatbox Media Steward MCP with config: %s",
        json.dumps(settings.redacted_summary(), sort_keys=True),
    )
    uvicorn.run(create_app(settings), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
