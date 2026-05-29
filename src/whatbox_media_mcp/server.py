from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from whatbox_media_mcp.config import Settings, load_settings
from whatbox_media_mcp.runtime import Services, build_services
from whatbox_media_mcp.tools.plex import plex_overview
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
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

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
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        return headers.get("authorization") == f"Bearer {self.token}"


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
        return await radarr_overview(services, include_movies, include_queue, include_missing, limit)

    async def sonarr_overview_tool(
        include_series: bool = True,
        include_queue: bool = True,
        include_missing: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await sonarr_overview(services, include_series, include_queue, include_missing, limit)

    async def plex_overview_tool(
        section: str = "all",
        include_activity: bool = True,
        include_recently_added: bool = True,
        include_staleness: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await plex_overview(
            services, section, include_activity, include_recently_added, include_staleness, limit
        )

    async def media_search_tool(
        query: str,
        types: list[str] | None = None,
        include_existing: bool = True,
        include_external_lookup: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await media_search(services, query, types, include_existing, include_external_lookup, limit)

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
        return await sonarr_research_series(services, sonarr_id, mode, confirm)

    async def radarr_queue_action_tool(
        queue_id: int,
        action: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await radarr_queue_action(services, queue_id, action, confirm)

    async def sonarr_queue_action_tool(
        queue_id: int,
        action: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await sonarr_queue_action(services, queue_id, action, confirm)

    async def radarr_delete_movie_tool(
        radarr_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await radarr_delete_movie(services, radarr_id, delete_files, add_import_exclusion, confirm)

    async def sonarr_delete_series_tool(
        sonarr_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await sonarr_delete_series(services, sonarr_id, delete_files, add_import_exclusion, confirm)

    async def staleness_report_tool(
        media_type: str = "all",
        older_than_days: int = 90,
        include_unwatched: bool = True,
        include_unmanaged: bool = True,
        include_missing: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await staleness_report(
            services,
            media_type,
            older_than_days,
            include_unwatched,
            include_unmanaged,
            include_missing,
            limit,
        )

    register_tool(mcp, "media_status", READ_ONLY, media_status_tool)
    register_tool(mcp, "radarr_overview", READ_ONLY, radarr_overview_tool)
    register_tool(mcp, "sonarr_overview", READ_ONLY, sonarr_overview_tool)
    register_tool(mcp, "plex_overview", READ_ONLY, plex_overview_tool)
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


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or load_settings()
    services = build_services(settings)
    mcp = create_mcp(services)
    try:
        app = mcp.http_app(path="/mcp")
    except TypeError:
        app = mcp.http_app()
    return BearerAuthApp(app, settings.mcp_bearer_token.get_secret_value())


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
