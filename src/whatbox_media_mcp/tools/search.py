from __future__ import annotations

from typing import Any

from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import clamp_limit, confidence, safe_tool


async def media_search(
    services: Services,
    query: str,
    types: list[str] | None = None,
    include_existing: bool = True,
    include_external_lookup: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not query.strip():
            return ToolResponse.failure("validation", "media_search requires a non-empty query.")
        bounded = clamp_limit(limit, default=10, maximum=50)
        wanted = set(types or ["movie", "series", "plex"])
        candidates: list[dict[str, Any]] = []
        if include_existing and "movie" in wanted:
            candidates.extend(await _radarr_existing(services, query))
        if include_external_lookup and "movie" in wanted:
            candidates.extend(await _radarr_lookup(services, query))
        if include_existing and "series" in wanted:
            candidates.extend(await _sonarr_existing(services, query))
        if include_external_lookup and "series" in wanted:
            candidates.extend(await _sonarr_lookup(services, query))
        if include_existing and "plex" in wanted:
            candidates.extend(await _plex_existing(services, query, bounded))
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        return ToolResponse.success({"query": query, "candidates": candidates[:bounded]})

    return await safe_tool(run)


async def _radarr_existing(services: Services, query: str) -> list[dict[str, Any]]:
    movies = await services.radarr.get("/api/v3/movie")
    return [
        {
            "kind": "movie",
            "source": "radarr",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": True,
            "confidence": confidence(query, str(item.get("title", "")), item.get("year")),
            "radarr_id": item.get("id"),
            "tmdb_id": item.get("tmdbId"),
            "imdb_id": item.get("imdbId"),
        }
        for item in _as_list(movies)
        if confidence(query, str(item.get("title", "")), item.get("year")) >= 0.45
    ]


async def _radarr_lookup(services: Services, query: str) -> list[dict[str, Any]]:
    movies = await services.radarr.get("/api/v3/movie/lookup", {"term": query})
    return [
        {
            "kind": "movie",
            "source": "radarr_lookup",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": False,
            "confidence": confidence(query, str(item.get("title", "")), item.get("year")),
            "tmdb_id": item.get("tmdbId"),
            "imdb_id": item.get("imdbId"),
        }
        for item in _as_list(movies)
    ]


async def _sonarr_existing(services: Services, query: str) -> list[dict[str, Any]]:
    series = await services.sonarr.get("/api/v3/series")
    return [
        {
            "kind": "series",
            "source": "sonarr",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": True,
            "confidence": confidence(query, str(item.get("title", "")), item.get("year")),
            "sonarr_id": item.get("id"),
            "tvdb_id": item.get("tvdbId"),
            "imdb_id": item.get("imdbId"),
        }
        for item in _as_list(series)
        if confidence(query, str(item.get("title", "")), item.get("year")) >= 0.45
    ]


async def _sonarr_lookup(services: Services, query: str) -> list[dict[str, Any]]:
    series = await services.sonarr.get("/api/v3/series/lookup", {"term": query})
    return [
        {
            "kind": "series",
            "source": "sonarr_lookup",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": False,
            "confidence": confidence(query, str(item.get("title", "")), item.get("year")),
            "tvdb_id": item.get("tvdbId"),
            "imdb_id": item.get("imdbId"),
        }
        for item in _as_list(series)
    ]


async def _plex_existing(services: Services, query: str, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for section_name in [services.settings.plex_movie_section, services.settings.plex_tv_section]:
        for item in await services.plex.search(section_name, query, limit):
            candidates.append(
                {
                    "kind": "plex_item",
                    "source": "plex",
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "exists": True,
                    "confidence": confidence(query, str(item.get("title", "")), item.get("year")),
                    "plex_rating_key": item.get("rating_key"),
                }
            )
    return candidates


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
