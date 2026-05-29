# agent memory

## Current focus

Initial implementation of the Whatbox Media Steward MCP service is complete through the planned scaffold, read/write tool surface, mocked tests, and docs.


## Design decisions

- Use a small Python 3.11+ FastMCP service with `uv` and `just` as the central command interface.
- Keep upstream access behind fixed Radarr/Sonarr/Plex/Tautulli clients; no arbitrary URLs, filesystem access, shell execution, torrent clients, or indexer integrations.
- Require bearer auth for `/mcp`; keep `/health` unauthenticated for local and Whatbox smoke checks.
- Use `confirm=false` dry-runs for all write tools, exact internal IDs for delete/re-search, TMDb/TVDb IDs for add, and `delete_files=false` defaults.
- Tautulli is optional and only enriches Plex overview when enabled.

## Current state

- Added project scaffold: `pyproject.toml`, `justfile`, `.env.example`, `.gitignore`, scripts, package modules, tests, and README.
- Implemented settings validation/redaction, shared tool response schemas, typed errors, service factory, Arr client, Plex wrapper, optional Tautulli client, and FastMCP registration.
- Implemented tools: `media_status`, `radarr_overview`, `sonarr_overview`, `plex_overview`, `media_search`, Radarr/Sonarr add, delete, re-search, and `staleness_report`.
- Added mocked tests for config, Arr client behavior, tool safety defaults, partial status, search, duplicate add, command validation, and absence of non-goal tool names.
- Verification passed with `just check`: ruff, mypy, and 13 pytest tests.

## Known issues

- No live Whatbox/Radarr/Sonarr/Plex smoke test has been run because real credentials and managed-link details are not configured.
- MCP transport/auth has only been covered by import/registration tests so far; next step should include an ASGI-level `/mcp` auth test and live local startup smoke.

## Next task

Configure a real `.env`, run the server locally, verify `/health`, then connect to live Radarr/Sonarr/Plex with read-only tools before testing any confirmed write operation.
