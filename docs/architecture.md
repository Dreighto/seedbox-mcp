# Architecture

What runs in this repo and how the code is layered. Agent rules are in
`AGENTS.md`; config, deployment and example prompts are in `README.md`.

## What runs

Not just an MCP server. Two co-hosted services plus several unattended daemons,
all in `src/seedbox_mcp/`:

- **MCP server** (FastMCP over Starlette/uvicorn, `/mcp` endpoint): about 50
  tools over Plex, Radarr, Sonarr, Prowlarr, SABnzbd, Tautulli, Jellyseerr,
  AdGuard, Gotify, Tdarr, Uptime Kuma, the in-house **NASDOOM** BFF, and NAS
  host-level ops over SSH.
- **Chat surface** (`seedbox-chat`): Plex-authenticated web chat for family
  members without a Claude account.
- **Two Telegram bots**: operator-only (`@nas_doombot`) and friend-facing
  (`@nasdoom_helperbot`), each allowlisted by `chat_id`. The friend bot asks
  Claude on the operator's subscription first and falls back to a local Ollama
  model; everything else asks Ollama Cloud first and falls back to the
  subscription models on a 429 (`chat/subscription_models.py`).
- **Unattended daemons**: `monitor.py` (the check-cycle triage loop, see
  `docs/check-cycle-triage-design.md`), `digest.py`, and the friend-notify and
  error-watch jobs.

## Three layers, kept separate

- `server.py`: FastMCP tool registration only. Its docstrings are the
  model-facing tool contract (router rules like "tmdb_id must come from a
  media_search candidate, never recall or construct one"); treat them with the
  same care as user-facing copy, not as internal comments.
- `tools/`: the tool logic: business rules, dry-run/confirm gating, response
  shaping.
- `clients/`: thin HTTP wrappers per upstream service, no business logic.
- `runtime.py` (`build_services()`) wires config to clients to tools. New
  integrations follow this dependency-injection pattern, not ad-hoc
  instantiation inside a tool.
- **Prefer NASDOOM's consolidated tools over raw per-service calls** for
  anything it already unifies (queue, requests, storage with its denominator,
  cross-source search), as `config.py` states. Don't reinvent a raw client call
  NASDOOM already consolidates.

## Tech stack

Python 3.11 or newer, `uv` for dependencies and the lockfile, `just` for tasks
(`just setup / run / test / check / deploy`). `fastmcp>=2`, `starlette` and
`uvicorn`; `pydantic` and `pydantic-settings` for config; `httpx` for all HTTP,
`respx` to mock it in tests; `rapidfuzz` for fuzzy search. mypy runs with
`strict = true` (tests excluded) and ruff with line length 120
(`pyproject.toml`).
