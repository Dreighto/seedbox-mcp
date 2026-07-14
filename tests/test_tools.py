from __future__ import annotations

import pytest

from seedbox_mcp.runtime import Services
from seedbox_mcp.tools.common import compact_tautulli_activity
from seedbox_mcp.tools.plex import plex_overview
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
    sonarr_monitor_season,
    sonarr_overview,
    sonarr_queue_action,
    sonarr_research_series,
)
from seedbox_mcp.tools.staleness import staleness_report
from seedbox_mcp.tools.status import media_status
from tests.conftest import FakeArrClient


@pytest.mark.asyncio
async def test_media_status_returns_partial_success(settings) -> None:  # type: ignore[no-untyped-def]
    class FailingPlex:
        async def get_sections(self):  # type: ignore[no-untyped-def]
            from seedbox_mcp.errors import UpstreamError

            raise UpstreamError("upstream_unreachable", "Plex down.")

        async def get_sessions(self):  # type: ignore[no-untyped-def]
            return []

    radarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "5"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [],
        }
    )
    sonarr = FakeArrClient(
        {
            ("GET", "/api/v3/system/status"): {"version": "4"},
            ("GET", "/api/v3/health"): [],
            ("GET", "/api/v3/diskspace"): [],
        }
    )
    result = await media_status(Services(settings, radarr, sonarr, FailingPlex()))  # type: ignore[arg-type]
    assert result["ok"] is True
    assert result["data"]["plex"]["reachable"] is False
    assert result["warnings"]


@pytest.mark.asyncio
async def test_media_search_returns_existing_and_lookup_candidates(services: Services) -> None:
    result = await media_search(services, "Heat", limit=10)
    assert result["ok"] is True
    sources = {item["source"] for item in result["data"]["candidates"]}
    assert {"radarr", "radarr_lookup", "plex"} <= sources


@pytest.mark.asyncio
async def test_radarr_add_dry_run_does_not_post(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    result = await radarr_add_movie(services, tmdb_id=1538, confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.radarr.posts == []


@pytest.mark.asyncio
async def test_radarr_add_duplicate_returns_existing(services: Services) -> None:
    result = await radarr_add_movie(services, tmdb_id=949, confirm=True)
    assert result["data"]["action"] == "already_exists"
    assert services.radarr.posts == []


@pytest.mark.asyncio
async def test_radarr_delete_defaults_to_preserve_files(services: Services) -> None:
    result = await radarr_delete_movie(services, radarr_id=1, confirm=True)
    assert result["ok"] is True
    assert services.radarr.deletes[0][1] == {"deleteFiles": "false", "addImportExclusion": "false"}


@pytest.mark.asyncio
async def test_sonarr_add_dry_run_does_not_post(services: Services) -> None:
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    result = await sonarr_add_series(services, tvdb_id=79126, confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.sonarr.posts == []


@pytest.mark.asyncio
async def test_sonarr_overview_includes_seasons_when_requested(services: Services) -> None:
    result = await sonarr_overview(services, include_queue=False, include_missing=False, include_seasons=True)
    series = result["data"]["series"][0]
    seasons = {s["season_number"]: s for s in series["seasons"]}
    assert seasons[1]["monitored"] is True
    assert seasons[1]["episode_file_count"] == 13
    assert seasons[2]["monitored"] is False
    assert seasons[2]["episode_file_count"] == 0


@pytest.mark.asyncio
async def test_sonarr_overview_omits_seasons_by_default(services: Services) -> None:
    result = await sonarr_overview(services, include_queue=False, include_missing=False)
    assert "seasons" not in result["data"]["series"][0]


@pytest.mark.asyncio
async def test_sonarr_monitor_season_dry_run_makes_no_change(services: Services) -> None:
    result = await sonarr_monitor_season(services, sonarr_id=2, season_number=2, confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert result["data"]["would_monitor"]["already_monitored"] is False
    assert services.sonarr.puts == []
    assert services.sonarr.posts == []


@pytest.mark.asyncio
async def test_sonarr_monitor_season_confirm_puts_and_searches(services: Services) -> None:
    result = await sonarr_monitor_season(services, sonarr_id=2, season_number=2, confirm=True)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is False
    # Series PUT back with the target season flipped monitored and series itself monitored.
    put_path, put_body = services.sonarr.puts[0]
    assert put_path == "/api/v3/series/2"
    assert put_body["monitored"] is True
    target = next(s for s in put_body["seasons"] if s["seasonNumber"] == 2)
    assert target["monitored"] is True
    # SeasonSearch command issued for that season.
    post_path, post_body = services.sonarr.posts[0]
    assert post_path == "/api/v3/command"
    assert post_body == {"name": "SeasonSearch", "seriesId": 2, "seasonNumber": 2}


@pytest.mark.asyncio
async def test_sonarr_monitor_season_no_search_when_disabled(services: Services) -> None:
    await sonarr_monitor_season(services, sonarr_id=2, season_number=2, search_now=False, confirm=True)
    assert services.sonarr.puts
    assert services.sonarr.posts == []


@pytest.mark.asyncio
async def test_sonarr_monitor_season_unknown_season_is_not_found(services: Services) -> None:
    result = await sonarr_monitor_season(services, sonarr_id=2, season_number=99, confirm=True)
    assert result["ok"] is False
    assert result["error_type"] == "not_found"
    assert result["details"]["available_seasons"] == [1, 2]
    assert services.sonarr.puts == []


@pytest.mark.asyncio
async def test_sonarr_delete_defaults_to_preserve_files(services: Services) -> None:
    result = await sonarr_delete_series(services, sonarr_id=2, confirm=True)
    assert result["ok"] is True
    assert services.sonarr.deletes[0][1] == {
        "deleteFiles": "false",
        "addImportListExclusion": "false",
    }


@pytest.mark.asyncio
async def test_radarr_queue_action_dry_run_does_not_delete(services: Services) -> None:
    result = await radarr_queue_action(services, queue_id=10, action="remove", confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.radarr.deletes == []


@pytest.mark.asyncio
async def test_radarr_queue_action_confirm_calls_delete(services: Services) -> None:
    result = await radarr_queue_action(services, queue_id=10, action="remove", confirm=True)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is False
    path, params = services.radarr.deletes[0]
    assert path == "/api/v3/queue/10"
    assert params == {"removeFromClient": "false", "blocklist": "false"}


@pytest.mark.asyncio
async def test_radarr_queue_action_blocklist_sets_flag(services: Services) -> None:
    await radarr_queue_action(services, queue_id=10, action="blocklist", confirm=True)
    _, params = services.radarr.deletes[0]
    assert params == {"removeFromClient": "false", "blocklist": "true"}


@pytest.mark.asyncio
async def test_sonarr_queue_action_dry_run_does_not_delete(services: Services) -> None:
    result = await sonarr_queue_action(services, queue_id=20, action="remove", confirm=False)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert services.sonarr.deletes == []


@pytest.mark.asyncio
async def test_sonarr_queue_action_confirm_calls_delete(services: Services) -> None:
    result = await sonarr_queue_action(services, queue_id=20, action="remove", confirm=True)
    assert result["ok"] is True
    path, params = services.sonarr.deletes[0]
    assert path == "/api/v3/queue/20"
    assert params == {"removeFromClient": "false", "blocklist": "false"}


@pytest.mark.asyncio
async def test_radarr_queue_item_exposes_clean_title_and_id(services: Services) -> None:
    result = await radarr_overview(services, include_movies=False, include_missing=False)
    item = result["data"]["queue"][0]
    assert item["title"] == "Heat"
    assert item["release_title"] == "Heat.1995.1080p.BluRay.x264-GROUP"
    assert item["radarr_id"] == 1


@pytest.mark.asyncio
async def test_sonarr_queue_item_exposes_clean_title_and_id(services: Services) -> None:
    result = await sonarr_overview(services, include_series=False, include_missing=False)
    item = result["data"]["queue"][0]
    assert item["title"] == "The Wire"
    assert item["release_title"] == "The.Wire.S01E01.1080p.BluRay.x264-GROUP"
    assert item["sonarr_id"] == 2


@pytest.mark.asyncio
async def test_queue_action_rejects_unknown_action(services: Services) -> None:
    result = await radarr_queue_action(services, queue_id=10, action="nuke")
    assert result["ok"] is False
    assert result["error_type"] == "validation"


@pytest.mark.asyncio
async def test_plex_overview_returns_sections_and_activity(services: Services) -> None:
    result = await plex_overview(services, section="all", include_activity=True, include_recently_added=True, limit=10)
    assert result["ok"] is True
    assert "Movies" in result["data"]["sections"]
    assert isinstance(result["data"]["recently_added"], list)
    assert isinstance(result["data"]["active_sessions"], list)


def test_compact_tautulli_activity_keeps_decision_shape_and_drops_noise() -> None:
    raw = {
        "stream_count": "2",
        "stream_count_direct_play": "0",
        "stream_count_direct_stream": "0",
        "stream_count_transcode": "2",
        "total_bandwidth": 86024,
        "lan_bandwidth": 0,
        "wan_bandwidth": 86024,
        "sessions": [
            {
                "session_key": "166",
                "media_type": "movie",
                "title": "Stray Dog",
                "parent_title": "",
                "grandparent_title": "",
                "year": "1949",
                "user": "alice",
                "state": "paused",
                "progress_percent": "5",
                "view_offset": "404000",
                "duration": "7364731",
                "rating_key": "7555",
                "library_name": "Movies",
                "player": "Chrome",
                "platform": "chrome",
                "product": "Plex Web",
                "location": "wan",
                "bandwidth": "43012",
                "quality_profile": "Original",
                "transcode_decision": "transcode",
                "video_decision": "copy",
                "audio_decision": "transcode",
                "subtitle_decision": "transcode",
                "directors": ["Akira Kurosawa"],
                "actors": ["Toshirō Mifune"],
                "genres": ["Crime", "Drama"],
                "file": "/home/user/files/movies/Stray Dog.mkv",
                "file_size": "19207843120",
                "ip_address": "203.0.113.42",
                "ip_address_public": "203.0.113.42",
                "email": "alice@example.com",
                "machine_id": "examplemachineid0000000000",
                "transcode_hw_decode": "",
                "transcode_hw_encode": "",
                "video_codec_level": "41",
                "stream_video_bitrate": "20417",
                "summary": "A detective loses his gun...",
            }
        ],
    }
    out = compact_tautulli_activity(raw)
    assert out["stream_count"] == "2"
    assert out["total_bandwidth"] == 86024
    assert len(out["sessions"]) == 1
    session = out["sessions"][0]
    assert session["title"] == "Stray Dog"
    assert session["user"] == "alice"
    assert session["state"] == "paused"
    assert session["transcode_decision"] == "transcode"
    assert session["video_decision"] == "copy"
    assert session["bandwidth"] == "43012"
    # Empty strings dropped per drop-empties rule.
    assert "parent_title" not in session
    assert "grandparent_title" not in session
    # PII / credits / file paths / transcoder internals all removed.
    for noisy in (
        "directors",
        "actors",
        "genres",
        "file",
        "file_size",
        "ip_address",
        "ip_address_public",
        "email",
        "machine_id",
        "transcode_hw_decode",
        "transcode_hw_encode",
        "video_codec_level",
        "stream_video_bitrate",
        "summary",
    ):
        assert noisy not in session


def test_compact_tautulli_activity_handles_empty() -> None:
    assert compact_tautulli_activity({}) == {
        "stream_count": None,
        "stream_count_direct_play": None,
        "stream_count_direct_stream": None,
        "stream_count_transcode": None,
        "total_bandwidth": None,
        "lan_bandwidth": None,
        "wan_bandwidth": None,
        "sessions": [],
    }
    assert compact_tautulli_activity(None) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_staleness_report_returns_expected_categories(services: Services) -> None:
    result = await staleness_report(services, media_type="movies", older_than_days=1, limit=10)
    assert result["ok"] is True
    data = result["data"]
    assert "added_long_ago_unwatched" in data
    assert "watched_long_ago" in data
    assert "managed_missing_from_plex" in data
    assert "queue_warnings" in data


@pytest.mark.asyncio
async def test_radarr_research_confirm_posts_command(services: Services) -> None:
    result = await radarr_research_movie(services, radarr_id=1, mode="search", confirm=True)
    assert result["ok"] is True
    assert result["data"]["dry_run"] is False
    path, payload = services.radarr.posts[0]
    assert path == "/api/v3/command"
    assert payload == {"name": "MoviesSearch", "movieIds": [1]}


@pytest.mark.asyncio
async def test_radarr_research_scan_downloaded_targets_the_movie(services: Services) -> None:
    # Regression test: scan_downloaded used to map to Radarr's
    # DownloadedMoviesScan command with no movieIds at all, which Radarr
    # rejects outright ("A path must be provided") since that command scans
    # the whole library, not one movie. Fixed 2026-07-13 to use RescanMovie,
    # Radarr's actual per-movie rescan command, scoped like every other mode.
    result = await radarr_research_movie(services, radarr_id=1, mode="scan_downloaded", confirm=True)
    assert result["ok"] is True
    _, payload = services.radarr.posts[0]
    assert payload == {"name": "RescanMovie", "movieIds": [1]}


@pytest.mark.asyncio
async def test_research_tools_allow_only_known_commands(services: Services) -> None:
    radarr_result = await radarr_research_movie(services, radarr_id=1, mode="unsupported")
    sonarr_result = await sonarr_research_series(services, sonarr_id=2, mode="unsupported")
    assert radarr_result["ok"] is False
    assert sonarr_result["ok"] is False
    assert radarr_result["error_type"] == "validation"
    assert sonarr_result["error_type"] == "validation"


# Radarr batch fixture: three movies with known sizes (in bytes).
def _radarr_batch_movies() -> list[dict]:
    return [
        {"id": 1, "title": "Heat", "year": 1995, "path": "/media/Movies/Heat", "sizeOnDisk": 10 * 1024**3},
        {"id": 2, "title": "Argo", "year": 2012, "path": "/media/Movies/Argo", "sizeOnDisk": 15 * 1024**3},
        {
            "id": 3,
            "title": "The Disaster Artist",
            "year": 2017,
            "path": "/media/Movies/The Disaster Artist",
            "sizeOnDisk": 5 * 1024**3,
        },
    ]


@pytest.mark.asyncio
async def test_radarr_delete_movies_batch_dry_run(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = _radarr_batch_movies()
    result = await radarr_delete_movies_batch(services, radarr_ids=[1, 2, 99], confirm=False)
    assert result["ok"] is True
    assert services.radarr.deletes == []
    data = result["data"]
    assert data["dry_run"] is True
    assert {row["radarr_id"] for row in data["would_delete"]} == {1, 2}
    assert data["not_found"] == [99]
    summary = data["summary"]
    assert summary == {"requested": 3, "found": 2, "not_found": 1, "estimated_size_gb": 25.0}


@pytest.mark.asyncio
async def test_radarr_delete_movies_batch_proceeds_past_failures(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = _radarr_batch_movies()
    services.radarr.delete_errors["/api/v3/movie/2"] = RuntimeError("boom")
    result = await radarr_delete_movies_batch(services, radarr_ids=[1, 2, 3, 99], delete_files=True, confirm=True)
    assert result["ok"] is True
    data = result["data"]
    assert data["dry_run"] is False
    # All three valid ids should have had a DELETE attempted, proving proceed-past-failures.
    attempted_paths = [path for path, _ in services.radarr.deletes]
    assert attempted_paths == ["/api/v3/movie/1", "/api/v3/movie/2", "/api/v3/movie/3"]
    assert {row["radarr_id"] for row in data["deleted"]} == {1, 3}
    failed_ids = {row["radarr_id"] for row in data["failed"]}
    assert failed_ids == {2, 99}
    summary = data["summary"]
    assert summary["requested"] == 4
    assert summary["deleted"] == 2
    assert summary["failed"] == 2
    # Only successful deletes count toward size freed (Heat 10 GB + Disaster Artist 5 GB = 15 GB).
    assert summary["total_size_deleted_gb"] == 15.0


@pytest.mark.asyncio
async def test_radarr_delete_movies_batch_passes_query_params(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = _radarr_batch_movies()
    await radarr_delete_movies_batch(
        services, radarr_ids=[1], delete_files=True, add_import_exclusion=True, confirm=True
    )
    _, params = services.radarr.deletes[0]
    assert params == {"deleteFiles": "true", "addImportExclusion": "true"}


@pytest.mark.asyncio
async def test_radarr_delete_movies_batch_rejects_empty(services: Services) -> None:
    result = await radarr_delete_movies_batch(services, radarr_ids=[], confirm=True)
    assert result["ok"] is False
    assert result["error_type"] == "validation"


def _sonarr_batch_series() -> list[dict]:
    return [
        {
            "id": 2,
            "title": "The Wire",
            "year": 2002,
            "path": "/media/TV/The Wire",
            "statistics": {"sizeOnDisk": 20 * 1024**3},
        },
        {
            "id": 4,
            "title": "Severance",
            "year": 2022,
            "path": "/media/TV/Severance",
            "statistics": {"sizeOnDisk": 8 * 1024**3},
        },
    ]


@pytest.mark.asyncio
async def test_sonarr_delete_series_batch_dry_run(services: Services) -> None:
    services.sonarr.routes[("GET", "/api/v3/series")] = _sonarr_batch_series()
    result = await sonarr_delete_series_batch(services, sonarr_ids=[2, 4, 99], confirm=False)
    assert result["ok"] is True
    assert services.sonarr.deletes == []
    data = result["data"]
    assert data["dry_run"] is True
    assert {row["sonarr_id"] for row in data["would_delete"]} == {2, 4}
    assert data["not_found"] == [99]
    assert data["summary"]["estimated_size_gb"] == 28.0


@pytest.mark.asyncio
async def test_sonarr_delete_series_batch_proceeds_past_failures(services: Services) -> None:
    services.sonarr.routes[("GET", "/api/v3/series")] = _sonarr_batch_series()
    services.sonarr.delete_errors["/api/v3/series/4"] = RuntimeError("boom")
    result = await sonarr_delete_series_batch(services, sonarr_ids=[2, 4], delete_files=True, confirm=True)
    assert result["ok"] is True
    attempted_paths = [path for path, _ in services.sonarr.deletes]
    assert attempted_paths == ["/api/v3/series/2", "/api/v3/series/4"]
    data = result["data"]
    assert {row["sonarr_id"] for row in data["deleted"]} == {2}
    assert {row["sonarr_id"] for row in data["failed"]} == {4}
    assert data["summary"]["total_size_deleted_gb"] == 20.0


@pytest.mark.asyncio
async def test_sonarr_delete_series_batch_passes_query_params(services: Services) -> None:
    services.sonarr.routes[("GET", "/api/v3/series")] = _sonarr_batch_series()
    await sonarr_delete_series_batch(
        services, sonarr_ids=[2], delete_files=True, add_import_exclusion=True, confirm=True
    )
    _, params = services.sonarr.deletes[0]
    assert params == {"deleteFiles": "true", "addImportListExclusion": "true"}


# Staleness sort + action-ID tests use a custom PlexClient so we can supply
# items with controllable timestamps and types.
class _StalenessPlex:
    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.offsets: list[int] = []

    async def get_sections(self) -> list[str]:
        return ["Movies", "TV Shows"]

    async def get_sections_by_type(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in self._items:
            sections = out.setdefault(item.get("type"), [])
            if item.get("section") not in sections:
                sections.append(item.get("section"))
        return out

    async def get_basic_library_items(self, section_name: str, limit: int, offset: int = 0) -> list[dict]:
        self.offsets.append(offset)
        return [item for item in self._items if item.get("section") == section_name][offset : offset + limit]


@pytest.mark.asyncio
async def test_staleness_report_attaches_radarr_id(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = [
        {"id": 42, "title": "Argo", "year": 2012, "hasFile": True},
    ]
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        {
            "type": "movie",
            "title": "Argo",
            "year": 2012,
            "section": "Movies",
            "rating_key": "100",
            "added_at": "2024-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 15.0,
        },
        {
            "type": "movie",
            "title": "Unknown Indie",
            "year": 2020,
            "section": "Movies",
            "rating_key": "101",
            "added_at": "2024-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 2.0,
        },
    ]
    object.__setattr__(services, "plex", _StalenessPlex(plex_items))
    result = await staleness_report(services, media_type="movies", older_than_days=1)
    assert result["ok"] is True
    items = result["data"]["added_long_ago_unwatched"]
    by_title = {item["title"]: item for item in items}
    assert by_title["Argo"]["radarr_id"] == 42
    assert by_title["Argo"]["match_status"] == "matched"
    assert by_title["Unknown Indie"]["radarr_id"] is None
    assert by_title["Unknown Indie"]["match_status"] == "unmanaged"
    # Verbosity trim: directors / file_paths / duration_minutes excluded.
    assert "directors" not in by_title["Argo"]
    assert "file_paths" not in by_title["Argo"]


@pytest.mark.asyncio
async def test_staleness_report_missing_from_plex_matches_by_file_path_not_title(services: Services) -> None:
    # Plex frequently displays a different matched title than Radarr's (region
    # subtitle variants, punctuation, etc). Matching on title alone produced
    # false positives for movies that are actually indexed and playable.
    services.radarr.routes[("GET", "/api/v3/movie")] = [
        {
            "id": 48,
            "title": "Alien Resurrection",
            "year": 1997,
            "hasFile": True,
            "movieFile": {"path": "/movies/Alien Resurrection (1997)/file.mkv"},
        },
        {
            "id": 99,
            "title": "Truly Missing Movie",
            "year": 2020,
            "hasFile": True,
            "movieFile": {"path": "/movies/Truly Missing Movie (2020)/file.mkv"},
        },
    ]
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        {
            "type": "movie",
            "title": "Alien: Resurrection",
            "year": 1997,
            "section": "Movies",
            "rating_key": "200",
            "added_at": "2024-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 8.0,
            "file_paths": ["/movies/Alien Resurrection (1997)/file.mkv"],
        },
    ]
    object.__setattr__(services, "plex", _StalenessPlex(plex_items))
    result = await staleness_report(services, media_type="movies", older_than_days=1)
    assert result["ok"] is True
    missing_ids = {item["radarr_id"] for item in result["data"]["managed_missing_from_plex"]}
    assert missing_ids == {99}


@pytest.mark.asyncio
async def test_staleness_report_missing_ignores_movies_in_secondary_plex_sections(services: Services) -> None:
    # Radarr manages both /movies and /anime-movies; Plex splits these into
    # separate libraries ("Movies", "Anime Movies"). A movie indexed in the
    # Anime Movies library must NOT be reported missing from Plex just because
    # it isn't in the primary "Movies" section — otherwise every anime movie
    # (100+ in the real library) is a false positive.
    services.radarr.routes[("GET", "/api/v3/movie")] = [
        {
            "id": 7,
            "title": "Akira",
            "year": 1988,
            "hasFile": True,
            "movieFile": {"path": "/anime-movies/Akira (1988)/akira.mkv"},
        },
        {
            "id": 8,
            "title": "Really Missing",
            "year": 2020,
            "hasFile": True,
            "movieFile": {"path": "/movies/Really Missing (2020)/rm.mkv"},
        },
    ]
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        {
            "type": "movie",
            "title": "Akira",
            "year": 1988,
            "section": "Anime Movies",
            "rating_key": "300",
            "added_at": "2024-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 5.0,
            "file_paths": ["/anime-movies/Akira (1988)/akira.mkv"],
        },
    ]
    object.__setattr__(services, "plex", _StalenessPlex(plex_items))
    result = await staleness_report(services, media_type="movies", older_than_days=1)
    assert result["ok"] is True
    missing_ids = {item["radarr_id"] for item in result["data"]["managed_missing_from_plex"]}
    assert missing_ids == {8}


@pytest.mark.asyncio
async def test_staleness_report_sort_staleness_desc(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        # Added long ago AND watched recently — most-recent-activity is the watch, so least stale.
        {
            "type": "movie",
            "title": "Recent Watch",
            "year": 2010,
            "section": "Movies",
            "rating_key": "1",
            "added_at": "2020-01-01T00:00:00+00:00",
            "last_viewed_at": "2026-05-01T00:00:00+00:00",
            "view_count": 1,
            "size_on_disk_gb": 1.0,
        },
        # Added recently, never watched.
        {
            "type": "movie",
            "title": "Recent Add",
            "year": 2024,
            "section": "Movies",
            "rating_key": "2",
            "added_at": "2025-12-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 1.0,
        },
        # Truly stale: added long ago, never watched.
        {
            "type": "movie",
            "title": "Truly Stale",
            "year": 2005,
            "section": "Movies",
            "rating_key": "3",
            "added_at": "2020-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 1.0,
        },
    ]
    object.__setattr__(services, "plex", _StalenessPlex(plex_items))
    result = await staleness_report(services, media_type="movies", older_than_days=30)
    # All three qualify for one of the two buckets. Default sort = staleness_desc.
    unwatched = result["data"]["added_long_ago_unwatched"]
    # Truly Stale (added 2020) comes before Recent Add (added 2025) in the unwatched bucket.
    titles = [i["title"] for i in unwatched]
    assert titles.index("Truly Stale") < titles.index("Recent Add")
    # Recent Watch is in watched_long_ago bucket only if its last_viewed_at < cutoff. With
    # older_than_days=30 from 2026-06-01, cutoff is 2026-05-02 and last_viewed_at is
    # 2026-05-01 — so it just qualifies as watched_long_ago.
    watched = result["data"]["watched_long_ago"]
    assert any(i["title"] == "Recent Watch" for i in watched)


@pytest.mark.asyncio
async def test_staleness_report_sort_size_desc_limit_after_sort(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        {
            "type": "movie",
            "title": f"Movie {i}",
            "year": 2000 + i,
            "section": "Movies",
            "rating_key": str(i),
            "added_at": "2020-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": float(i),
        }
        for i in range(1, 6)
    ]
    object.__setattr__(services, "plex", _StalenessPlex(plex_items))
    result = await staleness_report(services, media_type="movies", older_than_days=30, limit=2, sort="size_desc")
    titles = [i["title"] for i in result["data"]["added_long_ago_unwatched"]]
    # Limit applied after sort: top two by size, not alphabetically first two.
    assert titles == ["Movie 5", "Movie 4"]


@pytest.mark.asyncio
async def test_staleness_report_paginates_plex_library(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    plex_items = [
        {
            "type": "movie",
            "title": f"Movie {i}",
            "year": 2000,
            "section": "Movies",
            "rating_key": str(i),
            "added_at": "2020-01-01T00:00:00+00:00",
            "last_viewed_at": None,
            "view_count": 0,
            "size_on_disk_gb": 1.0,
        }
        for i in range(501)
    ]
    plex = _StalenessPlex(plex_items)
    object.__setattr__(services, "plex", plex)
    result = await staleness_report(services, media_type="movies", older_than_days=30, limit=500)
    titles = {item["title"] for item in result["data"]["added_long_ago_unwatched"]}
    assert len(titles) == 500
    assert plex.offsets == [0, 500]


@pytest.mark.asyncio
async def test_staleness_report_rejects_unknown_sort(services: Services) -> None:
    services.radarr.routes[("GET", "/api/v3/movie")] = []
    services.sonarr.routes[("GET", "/api/v3/series")] = []
    result = await staleness_report(services, sort="random")
    assert result["ok"] is False
    assert result["error_type"] == "validation"


@pytest.mark.asyncio
async def test_media_search_safe_for_action_exact_title_year(services: Services) -> None:
    result = await media_search(services, "Heat", year=1995, include_external_lookup=False, limit=10)
    radarr = next(c for c in result["data"]["candidates"] if c["source"] == "radarr")
    assert radarr["safe_for_action"] is True
    assert radarr["match_type"] == "exact_title_year"


@pytest.mark.asyncio
async def test_media_search_safe_for_action_year_mismatch_not_safe(services: Services) -> None:
    # The Radarr fixture has Heat (1995). Query with wrong year should not be safe.
    result = await media_search(services, "Heat", year=2024, include_external_lookup=False, limit=10)
    radarr_matches = [c for c in result["data"]["candidates"] if c["source"] == "radarr"]
    # year filter may exclude it entirely; if any remain they must be unsafe.
    for c in radarr_matches:
        assert c["safe_for_action"] is False


@pytest.mark.asyncio
async def test_media_search_lookup_candidates_never_safe(services: Services) -> None:
    result = await media_search(services, "Heat", year=1995, limit=10)
    for candidate in result["data"]["candidates"]:
        if candidate["source"] in {"radarr_lookup", "sonarr_lookup"}:
            assert candidate["safe_for_action"] is False


@pytest.mark.asyncio
async def test_media_search_external_lookup_runs_with_year(services: Services) -> None:
    # Regression: a year filter must not disable external lookup. The Radarr lookup
    # fixture returns Heat (1995), so a matching-year query should surface it.
    result = await media_search(services, "Heat", year=1995, limit=10)
    sources = {c["source"] for c in result["data"]["candidates"]}
    assert "radarr_lookup" in sources


@pytest.mark.asyncio
async def test_media_search_year_postfilters_lookup(services: Services) -> None:
    # Heat is 1995; a different year filter drops it from the lookup results.
    result = await media_search(services, "Heat", year=2024, limit=10)
    sources = {c["source"] for c in result["data"]["candidates"]}
    assert "radarr_lookup" not in sources


@pytest.mark.asyncio
async def test_media_search_crew_filter_keeps_lookup_and_warns(services: Services) -> None:
    result = await media_search(services, "Heat", actor="Al Pacino", limit=10)
    sources = {c["source"] for c in result["data"]["candidates"]}
    assert "radarr_lookup" in sources
    assert any("Plex only" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_media_search_drops_directors_when_no_filter(services: Services) -> None:
    result = await media_search(services, "Heat", limit=10)
    plex_candidates = [c for c in result["data"]["candidates"] if c["source"] == "plex"]
    assert plex_candidates
    for candidate in plex_candidates:
        assert "directors" not in candidate


@pytest.mark.asyncio
async def test_media_search_includes_directors_when_director_filter_set(services: Services) -> None:
    result = await media_search(services, "Heat", director="Michael Mann", limit=10)
    plex_candidates = [c for c in result["data"]["candidates"] if c["source"] == "plex"]
    assert plex_candidates
    for candidate in plex_candidates:
        assert "directors" in candidate
