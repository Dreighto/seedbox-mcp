from __future__ import annotations

from typing import Any

from whatbox_media_mcp.runtime import Services
from whatbox_media_mcp.schemas import ToolResponse
from whatbox_media_mcp.tools.common import clamp_limit, confidence, safe_tool


async def media_search(
    services: Services,
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
    async def run() -> dict[str, Any]:
        if not query and not any([director, actor, genre, language, year, country]):
            return ToolResponse.failure("validation", "media_search requires query or at least one attribute filter.")
        if query is not None and not query.strip():
            return ToolResponse.failure("validation", "media_search requires a non-empty query.")

        bounded = clamp_limit(limit, default=10, maximum=50)
        has_attr_filter = any([director, actor, genre, language, year, country])
        # Crew/location filters can only be applied via Plex; always include it when they're set.
        has_crew_filter = any([director, actor, country])
        effective_external = include_external_lookup and not has_attr_filter
        # Skip Radarr/Sonarr when crew filters are the only constraint — those fields aren't
        # in the Radarr/Sonarr list responses, so we'd return unfiltered results.
        # Radarr/Sonarr lack director/actor/country fields entirely, so including their
        # results when those filters are active would be misleading (genre-only partial match).
        skip_arr = has_crew_filter

        wanted = set(types or ["movie", "series", "plex"])
        if has_crew_filter:
            wanted.add("plex")

        candidates: list[dict[str, Any]] = []
        if include_existing and "movie" in wanted and not skip_arr:
            candidates.extend(await _radarr_existing(services, query, genre=genre, language=language, year=year))
        if effective_external and "movie" in wanted:
            candidates.extend(await _radarr_lookup(services, query))  # type: ignore[arg-type]
        if include_existing and "series" in wanted and not skip_arr:
            candidates.extend(await _sonarr_existing(services, query, genre=genre, year=year))
        if effective_external and "series" in wanted:
            candidates.extend(await _sonarr_lookup(services, query))  # type: ignore[arg-type]
        if include_existing and "plex" in wanted:
            candidates.extend(
                await _plex_existing(
                    services, query, bounded,
                    director=director, actor=actor, genre=genre,
                    language=language, year=year, country=country,
                )
            )
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        return ToolResponse.success({"query": query, "candidates": candidates[:bounded]})

    return await safe_tool(run)


async def _radarr_existing(
    services: Services,
    query: str | None,
    genre: str | None = None,
    language: str | None = None,
    year: int | None = None,
) -> list[dict[str, Any]]:
    movies = await services.radarr.get("/api/v3/movie")
    results = []
    for item in _as_list(movies):
        if genre and not _match_tag(genre, item.get("genres") or []):
            continue
        if language and not _match_str(language, (item.get("originalLanguage") or {}).get("name", "")):
            continue
        if year is not None and item.get("year") != year:
            continue
        title = str(item.get("title", ""))
        score = confidence(query, title, item.get("year")) if query else 1.0
        if query and score < 0.45:
            continue
        results.append({
            "kind": "movie",
            "source": "radarr",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": True,
            "confidence": score,
            "radarr_id": item.get("id"),
            "tmdb_id": item.get("tmdbId"),
            "imdb_id": item.get("imdbId"),
        })
    return results


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


async def _sonarr_existing(
    services: Services,
    query: str | None,
    genre: str | None = None,
    year: int | None = None,
) -> list[dict[str, Any]]:
    series = await services.sonarr.get("/api/v3/series")
    results = []
    for item in _as_list(series):
        if genre and not _match_tag(genre, item.get("genres") or []):
            continue
        if year is not None and item.get("year") != year:
            continue
        title = str(item.get("title", ""))
        score = confidence(query, title, item.get("year")) if query else 1.0
        if query and score < 0.45:
            continue
        results.append({
            "kind": "series",
            "source": "sonarr",
            "title": item.get("title"),
            "year": item.get("year"),
            "exists": True,
            "confidence": score,
            "sonarr_id": item.get("id"),
            "tvdb_id": item.get("tvdbId"),
            "imdb_id": item.get("imdbId"),
        })
    return results


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


async def _plex_existing(
    services: Services,
    query: str | None,
    limit: int,
    director: str | None = None,
    actor: str | None = None,
    genre: str | None = None,
    language: str | None = None,
    year: int | None = None,
    country: str | None = None,
) -> list[dict[str, Any]]:
    plex_filters: dict[str, Any] = {}
    if director:
        plex_filters["director="] = director
    if actor:
        plex_filters["actor="] = actor
    if genre:
        plex_filters["genre="] = genre
    if country:
        plex_filters["country="] = country
    if year is not None:
        plex_filters["year="] = year
    if language:
        plex_filters["audioLanguage="] = language

    candidates: list[dict[str, Any]] = []
    for section_name in [services.settings.plex_movie_section, services.settings.plex_tv_section]:
        for item in await services.plex.search(section_name, query, limit, plex_filters or None):
            score = confidence(query, str(item.get("title", "")), item.get("year")) if query else 1.0
            candidates.append({
                "kind": "plex_item",
                "source": "plex",
                "title": item.get("title"),
                "year": item.get("year"),
                "exists": True,
                "confidence": score,
                "plex_rating_key": item.get("rating_key"),
                "directors": item.get("directors"),
            })
    return candidates


def _match_tag(query_val: str, tags: list[str]) -> bool:
    q = query_val.lower()
    return any(q in tag.lower() for tag in tags)


def _match_str(query_val: str, value: str) -> bool:
    return query_val.lower() in value.lower()


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
