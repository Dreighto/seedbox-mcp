# Whatbox Media Steward MCP

Private remote MCP server for a Whatbox media stack. It exposes compact tools for Plex, Radarr, and Sonarr so ChatGPT can inspect media state and prepare conservative maintenance actions.

The service is intentionally narrow:

- Plex is read-only.
- Radarr and Sonarr writes are limited to add, re-search, and exact-ID delete.
- Write tools dry-run by default with `confirm=false`.
- Delete tools preserve files by default with `delete_files=false`.
- There are no torrent, indexer, filesystem, shell, scheduler, database, or frontend capabilities.

## Local Setup

```bash
cp .env.example .env
just setup
just run
```

The default bind is `127.0.0.1:17432`. The MCP endpoint is `/mcp`; a basic unauthenticated health check is available at `/health`.

Every `/mcp` request requires:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
```

## Configuration

All settings are read from `.env` or environment variables.

Required values:

- `MCP_BEARER_TOKEN`
- `RADARR_URL`
- `RADARR_API_KEY`
- `RADARR_DEFAULT_ROOT_FOLDER`
- `RADARR_DEFAULT_QUALITY_PROFILE_ID`
- `SONARR_URL`
- `SONARR_API_KEY`
- `SONARR_DEFAULT_ROOT_FOLDER`
- `SONARR_DEFAULT_QUALITY_PROFILE_ID`
- `PLEX_URL`
- `PLEX_TOKEN`

Optional Tautulli enrichment is enabled with:

```dotenv
TAUTULLI_ENABLED=true
TAUTULLI_URL=http://127.0.0.1:8181
TAUTULLI_API_KEY=...
```

Startup logs print a redacted config summary. API keys, Plex tokens, bearer tokens, and request headers are not logged.

## Command Interface

Use the `justfile` as the project interface:

```bash
just setup   # install runtime and dev dependencies with uv
just run     # run the MCP server
just test    # run pytest
just lint    # run ruff
just format  # format with ruff
just check   # lint, type-check, and test
just smoke   # call /health on the configured host/port
```

## Tool Surface

Read-only tools:

- `media_status`
- `radarr_overview`
- `sonarr_overview`
- `plex_overview`
- `media_search`
- `staleness_report`

Write tools:

- `radarr_add_movie`
- `radarr_research_movie`
- `radarr_delete_movie`
- `sonarr_add_series`
- `sonarr_research_series`
- `sonarr_delete_series`

Write tools require explicit identifiers where safety matters. Add operations require TMDb or TVDb IDs. Delete and re-search operations require exact Radarr or Sonarr internal IDs.

## Whatbox Deployment

Suggested layout on the slot:

```text
~/apps/whatbox-media-mcp/
  .env
  pyproject.toml
  src/
  scripts/
```

Install and run under the user account:

```bash
cd ~/apps/whatbox-media-mcp
uv sync --extra dev
scripts/run.sh
```

Configure a Whatbox managed HTTPS link to forward to:

```text
http://127.0.0.1:17432/mcp
```

Use the managed HTTPS URL as the public MCP endpoint in ChatGPT. Keep `MCP_BEARER_TOKEN` private and use it as the connected-app bearer token.

## ChatGPT Connection

Suggested app name:

```text
Whatbox Media Steward
```

Suggested model-facing instructions:

```text
Prefer read-only tools first. For add, delete, and re-search operations, first identify the exact media item with media_search or an overview tool. Never call delete tools with delete_files=true unless the user explicitly asked to delete files, not merely remove the item from Radarr or Sonarr. For ambiguous titles, return candidates or ask for disambiguation. Do not invent IDs. Do not claim to access torrent clients, indexers, shell, or the filesystem; this app intentionally does not expose those capabilities.
```

## Example Prompts

```text
What is currently playing on Plex?
```

```text
Are Radarr or Sonarr stuck on anything?
```

```text
Show movies added to Plex more than 90 days ago that no one has watched.
```

```text
Is Heat already on the server? If not, prepare to add the 1995 Michael Mann film, but do not actually add it yet.
```

```text
Remove the 2013 film The Heat from Radarr, but do not delete files.
```

## Troubleshooting

- Missing config: run `cp .env.example .env`, then fill in all required values.
- 401 from `/mcp`: verify `Authorization: Bearer <MCP_BEARER_TOKEN>`.
- Radarr or Sonarr auth errors: verify API keys in each app.
- Plex errors: verify `PLEX_URL`, `PLEX_TOKEN`, and section names.
- Partial status warnings: one upstream may be down while the MCP server itself is healthy.

Automated tests use mocked upstreams and do not require live media services.

