# Whatbox Media Steward MCP — Agent-Led Implementation Spec

## 1. Purpose

Build a small, private **remote MCP server** for a Whatbox slot that exposes a narrow media-management surface to ChatGPT as one connected app.

The app should act as a **media ops clerk**, not a fully autonomous seedbox controller:

- read broadly across Radarr, Sonarr, and Plex;
- diagnose stuck/missing/stale media states;
- add or re-search Radarr/Sonarr items only through explicit, typed tool calls;
- delete Radarr/Sonarr library records conservatively, with file deletion disabled by default;
- never expose torrent/indexer/filesystem/shell capabilities.

The core product experience should be:

> “Use ChatGPT to ask what is on Plex, what is stuck, what is stale, whether a film/show exists, and to prepare or confirm basic Radarr/Sonarr maintenance actions.”

---

## 2. Target Environment

### 2.1 Runtime

Target a **Whatbox slot** running a plain Python service.

Default implementation:

- Python virtualenv under the user account.
- No root privileges.
- No Docker/Podman dependency in v1.
- Long-running HTTP MCP server bound to loopback or slot-local interface.
- Exposed publicly through a Whatbox managed HTTPS link.

Suggested local layout:

```text
~/apps/whatbox-media-mcp/
  pyproject.toml
  .env
  README.md
  src/whatbox_media_mcp/
    __init__.py
    server.py
    config.py
    schemas.py
    clients/
      __init__.py
      arr.py
      plex.py
      tautulli.py
    tools/
      __init__.py
      status.py
      radarr.py
      sonarr.py
      plex.py
      search.py
      staleness.py
  scripts/
    run.sh
    healthcheck.sh
    install.sh
```

Suggested bind:

```text
127.0.0.1:17432
```

Public MCP endpoint:

```text
https://<managed-link-host>/mcp
```

---

## 3. Dependencies

Use a deliberately boring Python stack.

```toml
[project]
name = "whatbox-media-mcp"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastmcp>=2",
  "httpx>=0.28",
  "pydantic>=2",
  "pydantic-settings>=2",
  "python-dotenv>=1",
  "plexapi>=4",
  "rapidfuzz>=3"
]

[project.optional-dependencies]
dev = [
  "pytest",
  "pytest-asyncio",
  "respx",
  "ruff",
  "mypy"
]
```

Implementation preference:

- Use direct REST calls for Radarr/Sonarr.
- Use `plexapi` for Plex.
- Optionally use direct HTTP for Tautulli if enabled.
- Do not import broad third-party MCP media servers as runtime dependencies.

Prior art may be inspected for ideas, but the implementation should remain small and purpose-cut.

---

## 4. Configuration

Read config from `.env` and environment variables.

```dotenv
MCP_HOST=127.0.0.1
MCP_PORT=17432
MCP_PUBLIC_BASE_URL=https://media-mcp.example.box.ca
MCP_BEARER_TOKEN=change-me

RADARR_URL=http://127.0.0.1:7878
RADARR_API_KEY=change-me
RADARR_DEFAULT_ROOT_FOLDER=/home/user/files/Movies
RADARR_DEFAULT_QUALITY_PROFILE_ID=1
RADARR_DEFAULT_MIN_AVAILABILITY=released
RADARR_DELETE_FILES_DEFAULT=false

SONARR_URL=http://127.0.0.1:8989
SONARR_API_KEY=change-me
SONARR_DEFAULT_ROOT_FOLDER=/home/user/files/TV
SONARR_DEFAULT_QUALITY_PROFILE_ID=1
SONARR_DEFAULT_LANGUAGE_PROFILE_ID=
SONARR_DEFAULT_SERIES_TYPE=standard
SONARR_DELETE_FILES_DEFAULT=false

PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=change-me
PLEX_MOVIE_SECTION=Movies
PLEX_TV_SECTION=TV Shows

TAUTULLI_ENABLED=false
TAUTULLI_URL=http://127.0.0.1:8181
TAUTULLI_API_KEY=
```

Config requirements:

- Fail fast if Radarr/Sonarr/Plex URL or credentials are absent.
- Treat Tautulli as optional.
- Print a redacted config summary on startup.
- Never log raw API keys, Plex tokens, bearer tokens, or request headers.

---

## 5. Explicit Goals

1. Provide a single ChatGPT-connected MCP app for personal media-stack queries.
2. Wrap the minimum useful Radarr, Sonarr, and Plex functionality.
3. Use read-only tools for discovery and diagnosis.
4. Use write tools only for simple Radarr/Sonarr add/delete/re-search actions.
5. Require exact IDs for destructive operations.
6. Default all delete operations to “remove from Radarr/Sonarr only”, not “delete files”.
7. Make ambiguous title matching safe by returning candidates.
8. Provide clear compact outputs that are useful to an LLM and human.
9. Keep deployment possible on a plain Whatbox slot without privileged infrastructure.
10. Keep the code small enough for agent-led development, review, and maintenance.

---

## 6. Explicit Non-Goals

These are intentionally out of scope for v1.

### 6.1 No torrent-client control

Do not expose qBittorrent, ruTorrent, Deluge, Transmission, or any torrent-client API.

Out of scope:

- adding torrents;
- deleting torrents;
- pausing/resuming torrents;
- viewing tracker details;
- inspecting peers;
- manipulating categories/tags;
- passing magnet links.

Rationale: the torrent client is too low-level and too legally/operationally ambiguous for a ChatGPT-facing v1 surface.

### 6.2 No indexer or Prowlarr search

Do not expose Prowlarr, Jackett, NZBHydra, or indexer search.

Out of scope:

- searching indexers;
- selecting releases;
- passing release URLs;
- triggering manual grabs;
- exposing indexer names or credentials.

Rationale: Radarr/Sonarr already provide a safer semantic abstraction for search and acquisition.

### 6.3 No filesystem access

Do not expose arbitrary file browsing, deletion, rename, move, chmod, shell globbing, media-file inspection, or path reads.

Allowed exception:

- return paths already provided by Radarr/Sonarr/Plex metadata when useful for diagnosis.

Out of scope:

- listing directories;
- scanning the user’s home directory;
- deleting files directly;
- touching media files outside Radarr/Sonarr APIs.

### 6.4 No shell execution

Do not expose shell commands, process management, package installation, service restart, cron editing, or arbitrary command execution.

Out of scope:

- `bash`;
- `systemctl`;
- `pkill`;
- arbitrary Python execution;
- restarting Radarr/Sonarr/Plex;
- editing configs.

### 6.5 No broad admin panel

Do not attempt to manage the whole seedbox.

Out of scope:

- disk cleanup outside media summaries;
- VPN/proxy configuration;
- app installation;
- Whatbox account management;
- webserver/reverse proxy changes;
- managed-link automation.

### 6.6 No autonomous background daemon behaviour

The MCP server responds to ChatGPT tool calls. It does not independently decide to act.

Out of scope:

- scheduled cleanups;
- autonomous deleting;
- autonomous library pruning;
- autonomous adding based on recommendations;
- notification loops;
- polling workflows.

A separate scheduler may call ChatGPT or the MCP server later, but that is not part of v1.

### 6.7 No bulk destructive operations

Do not support bulk deletion or mass unmonitoring in v1.

Out of scope:

- “delete all unwatched movies over 20GB”;
- “remove all old shows”;
- “unmonitor everything missing”;
- “clear the queue”;
- “delete all failed downloads”.

The app may produce candidate lists for human review.

### 6.8 No recommendation engine as primary product

Recommendations are allowed only when grounded in actual Plex/Radarr/Sonarr state.

Out of scope:

- generic “what should I watch?” without library grounding;
- external review aggregation;
- Trakt/Letterboxd integration;
- taste modelling;
- watch-party planning.

### 6.9 No multi-user permission model

Assume this is a private single-user MCP server.

Out of scope:

- per-user permissions;
- multi-tenant auth;
- OAuth account linking;
- public app-store distribution;
- shared-family role separation.

### 6.10 No polished frontend

There is no web UI in v1.

Out of scope:

- React/Vue/Svelte UI;
- dashboards;
- interactive tables;
- mobile views;
- custom ChatGPT widgets.

ChatGPT is the interface.

### 6.11 No long-term state database

Do not add Postgres/SQLite/Redis unless a later feature absolutely requires it.

Allowed:

- in-memory request handling;
- optional tiny JSON cache for Plex/Tautulli expensive reads, if needed.

Out of scope:

- persistent user profiles;
- watch-history warehouse;
- background indexing;
- analytics database.

### 6.12 No direct modification of Plex library contents

Plex is read-only in v1.

Out of scope:

- deleting Plex items;
- editing metadata;
- changing posters;
- refreshing libraries;
- managing Plex users;
- changing sharing permissions;
- killing streams.

Allowed:

- read current activity;
- read library sections;
- read recently added;
- read metadata needed for staleness summaries.

---

## 7. Security and Safety Invariants

These are hard requirements.

1. ChatGPT never receives Radarr/Sonarr/Plex/Tautulli API keys.
2. The MCP server holds upstream API credentials in environment variables only.
3. The public MCP endpoint requires bearer-token authentication.
4. Tool implementations call fixed internal methods, not model-supplied URLs or paths.
5. Write tools must expose `confirm` semantics.
6. Destructive tools must require exact internal IDs.
7. `delete_files` defaults to `false`.
8. No tool can execute shell commands.
9. No tool can browse the filesystem.
10. No tool can call arbitrary URLs.
11. No tool can talk to torrent clients or indexers.
12. Logs must redact tokens and API keys.
13. Errors should be typed and compact.
14. Partial outages should return partial status instead of failing the whole request where possible.
15. Ambiguous media titles must return candidates rather than guessing.

---

## 8. Authentication Model

Use a simple bearer token for the private MCP endpoint.

Inbound request requirement:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Implementation notes:

- Reject missing/incorrect bearer token with 401.
- Do not support unauthenticated mode in production.
- Local development may allow `MCP_BEARER_TOKEN=dev` but still require a token.
- Do not implement OAuth in v1.

---

## 9. Core Data Types

### 9.1 Generic tool response

```json
{
  "ok": true,
  "data": {},
  "warnings": []
}
```

Error response:

```json
{
  "ok": false,
  "error_type": "not_found|ambiguous|upstream_unreachable|upstream_auth|validation|unsafe_request|unsupported",
  "message": "Human-readable compact explanation.",
  "details": {}
}
```

### 9.2 Media reference

```json
{
  "kind": "movie|series|plex_item",
  "source": "radarr|sonarr|plex|radarr_lookup|sonarr_lookup",
  "title": "Heat",
  "year": 1995,
  "exists": true,
  "confidence": 0.98,
  "radarr_id": 123,
  "sonarr_id": null,
  "plex_rating_key": null,
  "tmdb_id": 949,
  "tvdb_id": null,
  "imdb_id": "tt0113277"
}
```

### 9.3 Queue item summary

```json
{
  "source": "radarr|sonarr",
  "title": "Example",
  "status": "downloading|completed|failed|warning|unknown",
  "tracked_download_state": "downloading|importPending|importBlocked|failedPending|unknown",
  "progress_percent": 42.1,
  "estimated_completion_time": "2026-05-29T12:00:00Z",
  "error_message": null,
  "download_id": "redacted-or-omitted"
}
```

### 9.4 Plex item summary

```json
{
  "type": "movie|episode|show",
  "title": "Example",
  "year": 2020,
  "section": "Movies",
  "rating_key": "12345",
  "added_at": "2026-01-01T00:00:00Z",
  "last_viewed_at": null,
  "view_count": 0,
  "duration_minutes": 107,
  "file_paths": ["/home/user/files/Movies/Example/example.mkv"]
}
```

---

## 10. MCP Tool Catalogue

### 10.1 `media_status`

Purpose:

> Inspect whether Radarr, Sonarr, and Plex are reachable and healthy.

Inputs:

```json
{}
```

Output:

```json
{
  "radarr": {
    "reachable": true,
    "version": "5.x",
    "health": [],
    "disk": [{"path": "/home/user/files", "free_gb": 123.4}]
  },
  "sonarr": {
    "reachable": true,
    "version": "4.x",
    "health": [],
    "disk": [{"path": "/home/user/files", "free_gb": 123.4}]
  },
  "plex": {
    "reachable": true,
    "active_sessions": 1,
    "sections": ["Movies", "TV Shows"]
  }
}
```

Backend calls:

```text
Radarr GET /api/v3/system/status
Radarr GET /api/v3/health
Radarr GET /api/v3/diskspace

Sonarr GET /api/v3/system/status
Sonarr GET /api/v3/health
Sonarr GET /api/v3/diskspace

Plex  PlexServer.library.sections()
Plex  PlexServer.sessions()
```

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

### 10.2 `radarr_overview`

Purpose:

> Summarise Radarr movies, queue, missing items, and obvious stuck states.

Inputs:

```json
{
  "include_movies": true,
  "include_queue": true,
  "include_missing": true,
  "limit": 100
}
```

Backend calls:

```text
GET /api/v3/movie
GET /api/v3/queue?page=1&pageSize=<limit>
GET /api/v3/wanted/missing?page=1&pageSize=<limit>
```

Output should include compact summaries only.

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

### 10.3 `sonarr_overview`

Purpose:

> Summarise Sonarr series, queue, missing episodes, and obvious stuck states.

Inputs:

```json
{
  "include_series": true,
  "include_queue": true,
  "include_missing": true,
  "limit": 100
}
```

Backend calls:

```text
GET /api/v3/series
GET /api/v3/queue?page=1&pageSize=<limit>
GET /api/v3/wanted/missing?page=1&pageSize=<limit>
```

Output should include compact summaries only.

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

### 10.4 `plex_overview`

Purpose:

> Summarise Plex activity, library sections, recently added items, and basic staleness candidates.

Inputs:

```json
{
  "section": "movies|tv|all",
  "include_activity": true,
  "include_recently_added": true,
  "include_staleness": true,
  "limit": 100
}
```

Backend calls:

```text
PlexServer.sessions()
PlexServer.library.sections()
section.recentlyAdded(maxresults=<limit>)
section.search(...)
```

If Tautulli is enabled, enrich with:

```text
get_activity
get_history
get_recently_added
get_user_stats
```

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

### 10.5 `media_search`

Purpose:

> Search across existing Plex/Radarr/Sonarr state and Radarr/Sonarr external lookup APIs. Required before ambiguous add/delete operations.

Inputs:

```json
{
  "query": "string",
  "types": ["movie", "series", "plex"],
  "include_existing": true,
  "include_external_lookup": true,
  "limit": 10
}
```

Backend calls:

```text
Radarr existing: GET /api/v3/movie then local fuzzy filter
Radarr lookup:   GET /api/v3/movie/lookup?term=<query>

Sonarr existing: GET /api/v3/series then local fuzzy filter
Sonarr lookup:   GET /api/v3/series/lookup?term=<query>

Plex: library section search
```

Output:

```json
{
  "query": "heat",
  "candidates": [
    {
      "kind": "movie",
      "source": "radarr_lookup",
      "title": "Heat",
      "year": 1995,
      "tmdb_id": 949,
      "imdb_id": "tt0113277",
      "exists": false,
      "confidence": 0.93
    }
  ]
}
```

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

### 10.6 `radarr_add_movie`

Purpose:

> Add a movie to Radarr, optionally searching for it after creation.

Inputs:

```json
{
  "tmdb_id": 949,
  "title": "Heat",
  "year": 1995,
  "quality_profile_id": null,
  "root_folder": null,
  "minimum_availability": null,
  "monitored": true,
  "search_now": true,
  "confirm": false
}
```

Behaviour:

1. If `confirm=false`, return a dry-run payload and do not mutate state.
2. If `confirm=true`, resolve the TMDb ID, add movie to Radarr, and optionally search.
3. Use configured defaults when optional inputs are null.
4. Return created Radarr movie ID and relevant status.

Backend calls:

```text
GET  /api/v3/movie/lookup/tmdb?tmdbId=<tmdb_id>
POST /api/v3/movie
POST /api/v3/command {"name": "MoviesSearch", "movieIds": [<id>]}
```

Safety:

- Require `tmdb_id`.
- Do not infer ambiguous IDs from title alone.
- If a movie already exists, return existing record and do not duplicate.

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.7 `radarr_delete_movie`

Purpose:

> Remove a movie from Radarr. File deletion is disabled by default.

Inputs:

```json
{
  "radarr_id": 123,
  "delete_files": false,
  "add_import_exclusion": false,
  "confirm": false
}
```

Behaviour:

1. Require exact `radarr_id`.
2. Fetch the movie first and return a dry-run summary if `confirm=false`.
3. If `confirm=true`, delete the Radarr movie record.
4. Only delete files if `delete_files=true` and the user explicitly requested file deletion.

Backend calls:

```text
GET    /api/v3/movie/<radarr_id>
DELETE /api/v3/movie/<radarr_id>?deleteFiles=false&addImportExclusion=false
```

Safety:

- No bulk deletion.
- No title-only deletion.
- `delete_files=false` by default.
- If `delete_files=true` and `confirm=false`, return an unsafe-request error or explicit dry-run warning.

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.8 `radarr_research_movie`

Purpose:

> Re-search, refresh, or scan Radarr state for a specific movie.

Inputs:

```json
{
  "radarr_id": 123,
  "mode": "search|refresh|scan_downloaded",
  "confirm": false
}
```

Behaviour:

1. Require exact `radarr_id` for movie-specific actions.
2. If `confirm=false`, return dry-run command.
3. If `confirm=true`, post the command.

Backend mapping:

```text
mode=search:
  POST /api/v3/command {"name": "MoviesSearch", "movieIds": [<id>]}

mode=refresh:
  POST /api/v3/command {"name": "RefreshMovie", "movieIds": [<id>]}

mode=scan_downloaded:
  POST /api/v3/command {"name": "DownloadedMoviesScan"}
```

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.9 `sonarr_add_series`

Purpose:

> Add a series to Sonarr, optionally searching for missing episodes after creation.

Inputs:

```json
{
  "tvdb_id": 121361,
  "title": "Game of Thrones",
  "quality_profile_id": null,
  "root_folder": null,
  "series_type": "standard",
  "season_folder": true,
  "monitor": "future|missing|all|none",
  "search_now": true,
  "confirm": false
}
```

Behaviour:

1. If `confirm=false`, return a dry-run payload and do not mutate state.
2. If `confirm=true`, resolve the TVDb ID, add series to Sonarr, and optionally search.
3. Use configured defaults when optional inputs are null.

Backend calls:

```text
GET  /api/v3/series/lookup?term=tvdb:<tvdb_id>
POST /api/v3/series
POST /api/v3/command {"name": "SeriesSearch", "seriesId": <id>}
```

Safety:

- Require `tvdb_id`.
- Do not infer ambiguous IDs from title alone.
- If a series already exists, return existing record and do not duplicate.

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.10 `sonarr_delete_series`

Purpose:

> Remove a series from Sonarr. File deletion is disabled by default.

Inputs:

```json
{
  "sonarr_id": 456,
  "delete_files": false,
  "add_import_exclusion": false,
  "confirm": false
}
```

Behaviour:

1. Require exact `sonarr_id`.
2. Fetch the series first and return a dry-run summary if `confirm=false`.
3. If `confirm=true`, delete the Sonarr series record.
4. Only delete files if `delete_files=true` and the user explicitly requested file deletion.

Backend calls:

```text
GET    /api/v3/series/<sonarr_id>
DELETE /api/v3/series/<sonarr_id>?deleteFiles=false&addImportListExclusion=false
```

Safety:

- No bulk deletion.
- No title-only deletion.
- `delete_files=false` by default.

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.11 `sonarr_research_series`

Purpose:

> Re-search or refresh Sonarr state for a specific series.

Inputs:

```json
{
  "sonarr_id": 456,
  "mode": "series_search|refresh|missing_episode_search",
  "confirm": false
}
```

Behaviour:

1. Require exact `sonarr_id`.
2. If `confirm=false`, return dry-run command.
3. If `confirm=true`, post the command.

Backend mapping:

```text
mode=series_search:
  POST /api/v3/command {"name": "SeriesSearch", "seriesId": <id>}

mode=refresh:
  POST /api/v3/command {"name": "RefreshSeries", "seriesId": <id>}

mode=missing_episode_search:
  POST /api/v3/command {"name": "MissingEpisodeSearch", "seriesId": <id>}
```

Tool annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

---

### 10.12 `staleness_report`

Purpose:

> Identify stale, unwatched, unmanaged, missing, or inconsistent media state.

Inputs:

```json
{
  "media_type": "movies|tv|all",
  "older_than_days": 90,
  "include_unwatched": true,
  "include_unmanaged": true,
  "include_missing": true,
  "limit": 100
}
```

Behaviour:

Use Plex as the base library source. Enrich with Radarr/Sonarr where possible.

Report categories:

```text
- Plex items added long ago and never watched
- Plex items watched long ago and not recently revisited
- Plex items with no matching Radarr/Sonarr record
- Radarr/Sonarr monitored items missing from Plex
- Queue/import items stuck or warning
- Recently added but not yet watched
```

Safety:

- This is read-only.
- It may propose actions, but must not mutate state.

Tool annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

---

## 11. Client Implementation Requirements

### 11.1 Generic Arr client

Implement a generic async `ArrClient`.

```python
class ArrClient:
    def __init__(self, base_url: str, api_key: str):
        ...

    async def get(self, path: str, params: dict | None = None) -> dict | list:
        ...

    async def post(self, path: str, payload: dict) -> dict:
        ...

    async def delete(self, path: str, params: dict | None = None) -> dict | None:
        ...
```

Headers:

```http
X-Api-Key: <api-key>
Accept: application/json
Content-Type: application/json
```

HTTP behaviour:

- connect timeout: 5s
- read timeout: 30s
- write timeout: 10s
- raise typed upstream errors
- redact upstream URLs if they contain credentials
- do not expose raw upstream tracebacks in MCP output

### 11.2 Plex client

Use `plexapi`.

Required methods:

```python
get_sections()
get_sessions()
recently_added(section_name, limit)
search(section_name, query, limit)
get_basic_library_items(section_name, limit)
```

### 11.3 Tautulli client

Optional direct HTTP client.

Required methods if enabled:

```python
get_activity()
get_history(limit)
get_recently_added(limit)
get_user_stats()
```

If Tautulli is disabled or unreachable, Plex tools should continue with reduced functionality.

---

## 12. Agent-Led Build Tasks

Implement in this order. Do not skip acceptance tests for a stage before moving to the next.

### Stage 0 — scaffold

Tasks:

1. Create package structure.
2. Add `pyproject.toml`.
3. Add config loader with pydantic settings.
4. Add `.env.example`.
5. Add `scripts/run.sh`.
6. Add minimal FastMCP server with health endpoint or equivalent startup check.
7. Add ruff/pytest config.

Acceptance criteria:

- `python -m whatbox_media_mcp.server` starts.
- Missing required config produces readable validation errors.
- Tokens are redacted in logs.
- README has local setup instructions.

---

### Stage 1 — read-only spine

Tasks:

1. Implement `ArrClient`.
2. Implement Radarr status/health/disk calls.
3. Implement Sonarr status/health/disk calls.
4. Implement Plex sections/sessions calls.
5. Implement `media_status`.
6. Implement `radarr_overview`.
7. Implement `sonarr_overview`.
8. Implement `plex_overview`.
9. Implement compact output schemas.

Acceptance criteria:

- `media_status` returns partial success if one upstream is down.
- Overview tools paginate or limit outputs.
- Outputs are compact and do not dump full upstream JSON.
- All read tools have read-only annotations.

---

### Stage 2 — search

Tasks:

1. Implement Radarr existing-library search.
2. Implement Radarr external movie lookup.
3. Implement Sonarr existing-library search.
4. Implement Sonarr external series lookup.
5. Implement Plex library search.
6. Implement fuzzy candidate ranking with `rapidfuzz`.
7. Implement `media_search`.

Acceptance criteria:

- Ambiguous queries return multiple candidates.
- Existing items are marked as existing.
- Lookup candidates include stable external IDs where available.
- No mutation occurs in search tools.
- Search results are bounded by `limit`.

---

### Stage 3 — safe add/re-search writes

Tasks:

1. Implement `radarr_add_movie`.
2. Implement `radarr_research_movie`.
3. Implement `sonarr_add_series`.
4. Implement `sonarr_research_series`.
5. Add dry-run behaviour for all write tools.
6. Add duplicate detection before add.
7. Add exact-ID validation.

Acceptance criteria:

- `confirm=false` never mutates upstream services.
- `confirm=true` mutates only the requested Radarr/Sonarr entity.
- Duplicate add returns existing item rather than creating a duplicate.
- Add movie requires TMDb ID.
- Add series requires TVDb ID.
- Re-search requires exact Radarr/Sonarr ID.
- Tool annotations mark write status correctly.

---

### Stage 4 — conservative deletion

Tasks:

1. Implement `radarr_delete_movie`.
2. Implement `sonarr_delete_series`.
3. Add dry-run previews with title/path/monitored state.
4. Ensure `delete_files=false` default.
5. Add explicit unsafe-request handling for bulk/title-only deletion.

Acceptance criteria:

- Delete requires exact internal ID.
- Delete with `confirm=false` does not mutate.
- Delete with `delete_files=true` is clearly flagged and requires explicit confirmation.
- No bulk deletion path exists.
- No filesystem deletion path exists outside Radarr/Sonarr API semantics.

---

### Stage 5 — staleness report

Tasks:

1. Implement Plex item summary extraction.
2. Match Plex movies to Radarr movies.
3. Match Plex shows to Sonarr series.
4. Identify added-but-unwatched items.
5. Identify managed-but-missing items.
6. Identify Plex-unmanaged items.
7. Optionally enrich with Tautulli if enabled.
8. Implement `staleness_report`.

Acceptance criteria:

- Report is read-only.
- Report groups candidates by reason.
- Report includes enough IDs for follow-up actions.
- Report does not recommend destructive action as a command; it may suggest review.

---

### Stage 6 — deployment docs

Tasks:

1. Write Whatbox setup instructions.
2. Write managed-link setup instructions.
3. Write ChatGPT Developer Mode connection instructions.
4. Write `.env` documentation.
5. Write troubleshooting section.
6. Add example prompts.
7. Add security notes.

Acceptance criteria:

- A fresh user can install in a venv.
- A fresh user can expose `/mcp` over HTTPS.
- A fresh user can connect ChatGPT.
- A fresh user can run a smoke test.

---

## 13. Test Plan

Use mocked upstreams. Do not require live Radarr/Sonarr/Plex in CI.

### Unit tests

1. Config validation succeeds with complete `.env`.
2. Config validation fails with missing required secrets.
3. Secret redaction works.
4. Arr client sends `X-Api-Key`.
5. Arr client maps upstream 401 to `upstream_auth`.
6. Arr client maps timeout to `upstream_unreachable`.
7. Fuzzy search ranks exact title/year matches first.
8. Error responses conform to schema.

### Tool tests

1. `media_status` succeeds when all services respond.
2. `media_status` returns partial status when Plex fails.
3. `radarr_overview` limits movie/queue/missing output.
4. `sonarr_overview` limits series/queue/missing output.
5. `plex_overview` returns sessions and recently added items.
6. `media_search` returns existing and lookup candidates.
7. `radarr_add_movie(confirm=false)` does not call `POST /movie`.
8. `radarr_add_movie(confirm=true)` calls add and optional search.
9. `radarr_add_movie` refuses duplicate creation.
10. `sonarr_add_series(confirm=false)` does not call `POST /series`.
11. `sonarr_add_series(confirm=true)` calls add and optional search.
12. `radarr_delete_movie(confirm=false)` does not call DELETE.
13. `radarr_delete_movie(confirm=true)` calls DELETE with `deleteFiles=false`.
14. `sonarr_delete_series(confirm=true)` calls DELETE with `deleteFiles=false`.
15. Re-search tools post only allowed command names.
16. Staleness report is read-only.
17. Unsupported requests have no matching tool or return unsupported.

### Negative tests

Assert that no tool exists for:

- torrent client operations;
- Prowlarr/indexer search;
- arbitrary filesystem listing;
- shell execution;
- Plex metadata editing;
- bulk deletion.

---

## 14. Example Prompts for Manual Validation

```text
Use Whatbox Media Steward. What’s currently playing on Plex?
```

```text
Use Whatbox Media Steward. Are Radarr or Sonarr stuck on anything?
```

```text
Use Whatbox Media Steward. Show me movies added to Plex more than 90 days ago that no one has watched.
```

```text
Use Whatbox Media Steward. Is Heat already on the server? If not, prepare to add the 1995 Michael Mann film, but don’t actually add it yet.
```

```text
Use Whatbox Media Steward. Re-search The Wire in Sonarr, but show me the command before running it.
```

```text
Use Whatbox Media Steward. Remove the 2013 film The Heat from Radarr, but do not delete files.
```

Expected refusals or unsupported responses:

```text
Use Whatbox Media Steward. Search Prowlarr for a torrent of Heat.
```

```text
Use Whatbox Media Steward. Delete all unwatched movies over 20GB.
```

```text
Use Whatbox Media Steward. Run a shell command on the slot.
```

---

## 15. ChatGPT App Description

Suggested connected-app name:

```text
Whatbox Media Steward
```

Suggested description:

```text
Inspect and lightly manage a private Whatbox media stack. Provides read access to Plex activity/library, Radarr movie state, and Sonarr TV state. Can prepare or confirm adding, removing, and re-searching Radarr/Sonarr items. Cannot access torrent clients, indexers, the filesystem, shell, arbitrary websites, or Plex write operations.
```

Suggested model-facing instructions:

```text
Prefer read-only tools first. For add, delete, and re-search operations, first identify the exact media item with media_search or an overview tool. Never call delete tools with delete_files=true unless the user explicitly asked to delete files, not merely remove the item from Radarr or Sonarr. For ambiguous titles, return candidates or ask for disambiguation. Do not invent IDs. Do not claim to access torrent clients, indexers, shell, or the filesystem; this app intentionally does not expose those capabilities.
```

---

## 16. Definition of Done

The implementation is done when:

1. The MCP server runs on a Whatbox slot from a Python virtualenv.
2. The MCP endpoint is reachable through a Whatbox managed HTTPS link.
3. ChatGPT can connect to it as one app.
4. Read tools work against live Radarr/Sonarr/Plex.
5. Write tools perform dry-runs by default.
6. Add/re-search operations work when confirmed.
7. Delete operations require exact IDs and default to preserving files.
8. No non-goal capabilities are exposed.
9. Unit and mocked integration tests pass.
10. README documents setup, config, tool behaviour, safety model, and examples.

---

## 17. Implementation Notes for Agents

When implementing:

- Prefer fewer tools with clearer contracts over many thin wrappers.
- Prefer compact typed summaries over raw upstream JSON.
- Do not pass user/model strings into arbitrary URL paths.
- Use typed enums for write modes and command names.
- Keep dangerous concepts absent from the codebase rather than hidden behind flags.
- Make refusal/unsupported states explicit and boring.
- Do not add extra integrations unless the spec asks for them.
- Do not introduce a database unless a test requires persistent state.
- Keep the happy path legible in code review.
- Treat this as a private operations tool, not a public product.
