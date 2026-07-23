# Agent rules: seedbox-mcp (NAS/media ops MCP server + bots)

Every agent working in this repo (Claude Code, Codex, Gemini, Cursor, OpenCode, DeepSeek,
all of them) follows this file. Read it before touching anything. `README.md` is the
operator-facing reference (config, deployment, example prompts); this file is about agent
behavior, it doesn't restate that.

## What this is

Not just an MCP server. Two co-hosted services plus several unattended daemons, all in
`src/seedbox_mcp/`:

- **MCP server** (FastMCP over Starlette/uvicorn, `/mcp` endpoint): ~50 tools over Plex,
  Radarr, Sonarr, Prowlarr, SABnzbd, Tautulli, Jellyseerr, AdGuard, Gotify, Tdarr, Uptime
  Kuma, the in-house **NASDOOM** BFF, and NAS host-level ops over SSH.
- **Chat surface** (`seedbox-chat`): Plex-authenticated web chat for family members without
  a Claude account.
- **Two Telegram bots**: operator-only (`@nas_doombot`) and friend-facing
  (`@nasdoom_helperbot`), each hard-allowlisted by `chat_id` (an unlisted chat is silently
  dropped; add the chat_id explicitly, don't assume "the bot replies to anyone who finds it").
- **Unattended daemons**: `monitor.py` (check-cycle triage loop), `digest.py`, plus
  friend-notify/error-watch jobs.

Deployed on a Whatbox seedbox slot via `scripts/deploy.sh` (SSH + git pull + `uv sync` +
restart), self-healed by a 5-min cron watchdog (`@reboot` cron is unreliable across Whatbox
slot migrations; that's why the watchdog exists, not a redundant safety net).

## Architecture: three layers, keep them separate

- `server.py`: FastMCP tool registration ONLY. Docstrings here are the model-facing tool
  contract (router rules like "tmdb_id must come from a media_search candidate, never
  recall or construct one"); treat them with the same care as user-facing copy, not as
  internal comments.
- `tools/`: actual tool logic: business rules, dry-run/confirm gating, response shaping.
- `clients/`: thin HTTP wrappers per upstream service, no business logic.
- `runtime.py` (`build_services()`) wires config to clients to tools. New integrations follow
  this DI pattern, not ad-hoc instantiation inside a tool.
- **Prefer NASDOOM's consolidated tools over raw per-service calls** for anything it already
  unifies (queue, requests, storage-with-denominator, cross-source search), stated
  explicitly in `config.py`. Don't reinvent a raw client call NASDOOM already consolidates.

## The non-negotiable safety conventions (this is the real substance of this repo)

1. **Every mutating tool defaults to dry-run.** `confirm: bool = False` previews
   (`{"dry_run": True, "would_add": ...}`); only `confirm=True` executes. Delete tools
   additionally default `delete_files=False`. This is enforced across `radarr.py`,
   `sonarr.py`, `jellyseerr.py`, `nasdoom.py`, `host_health.py`; a new mutating tool that
   skips this pattern is a bug, not a style choice.
2. **Audit trail + circuit breaker are independent of model judgment.** `action_audit.py`
   appends one line per real (`confirm=true`) action to `.action_audit.jsonl`;
   `MAX_ACTIONS_PER_HOUR = 20` is a rolling-window breaker "a prompt can't talk its way
   around." Don't build a path that writes an action without going through this.
3. **Autonomous promotion is data-gated, not agent-judgment-gated.** `graduation.py` only
   _recommends_ a tool for the monitor's unattended set after real thresholds
   (`GRADUATION_MIN_SUCCESSES = 5`, `MIN_DISTINCT = 2`, 30-day recency); a human flips the
   switch. Don't hand-wire a tool into the autonomous set to skip this.
4. **Errors are `MediaMcpError`** (`errors.py`), `error_type` a closed `Literal`
   (`not_found`/`ambiguous`/`upstream_unreachable`/`upstream_auth`/`validation`/
   `unsafe_request`/`unsupported`). Tools raise this, never a bare exception, for anything
   client-facing.
5. **Secrets never get logged.** Every config secret is `SecretStr`; `redacted_summary()`
   masks anything matching `TOKEN|API_KEY|PASSWORD|SECRET|AUTHORIZATION` before it hits a
   startup log. New config fields with sensitive names inherit this automatically; don't
   bypass it by logging a raw value "just for debugging."
6. **A branch-scoped job stays branch-scoped.** `friend_error_watch.py`'s own header states
   it explicitly: "DO NOT push, DO NOT restart the live services, DO NOT deploy... The
   operator approves the deploy." Treat that as the general rule for any unattended-job code
   here, not a one-off comment.

## Traps that have actually bitten this repo (read before touching the related area)

- **Quality-profile enforcement is grab-time only, not import-time.** A release can be named
  like a normal 1080p encode and still land as BR-DISK or Remux post-download, confirmed
  live twice (_Super Mario Bros. 1993_ Remux, _Frankenstein 2025_ BR-DISK from three
  different indexer mirrors of the same release name). `quality_guard.py` re-checks landed
  quality post-import and reverts/blocklists after repeat offenses. Don't assume a
  disallowing quality profile alone is sufficient.
- **Generic `TELEGRAM_BOT_TOKEN` collides with a pre-existing shell-exported var** on this
  machine (a different bot's token silently wins over `.env` via `pydantic-settings`
  precedence). Confirmed live: a test digest went to the wrong bot. Config uses scoped var
  names for exactly this reason; don't rename toward something generic.
- **Download-strike system never acts on a single observation** (`download_strikes.py`),
  a deliberate anti-flakiness design. Also: SABnzbd's "Remove Completed" history setting can
  make completed downloads silently vanish from the strike system's visibility window before
  it warns at threshold (45).
- **Queue IDs can arrive as scientific-notation floats** from upstream JSON; must be coerced
  via `CoercedInt` in `schemas.py`. A real bug class for anything touching queue-action tools.
- **Test `Settings` fixtures must set `_env_file=None`**, or "default value" tests silently
  read the deployed `.env` instead of actual defaults (bit this repo once already, commit
  `b46a663`).
- **Bot tool-name uniqueness is enforced across "sections"**: a new `ACTION_TOOL` with a name
  that collides with another section's tool hard-fails. Check name uniqueness across bot
  sections before adding a tool, not just within the one you're editing.
- **The eval harness (`evals/bot_eval.py`) is a real sandbox, not a formality**: it drives
  the actual agent/prompt/tool-gate path but intercepts every write/escalation tool and
  patches out `record_action`/rate-limit checks specifically so sandbox runs can't pollute
  the graduation ledger or trip the real breaker. Don't skip running it because "it's just a
  script," and don't assume a passing eval run touched production state (it deliberately
  didn't).

## Tech stack

Python ≥3.11, `uv` for deps/lockfile, `just` for tasks (`just setup / run / test / check /
deploy`). `fastmcp>=2` + `starlette` + `uvicorn`, `pydantic`/`pydantic-settings` for config,
`httpx` for all HTTP, `respx` for HTTP mocking in tests, `rapidfuzz` for fuzzy search. mypy
**strict = true** (tests excluded), ruff line-length 120. No CI workflow exists; `just
check`/`just test` are the only gate, run them before claiming anything works. Tests tagged
`live` (`-m live`) hit real services and are gated behind `LIVE_TESTS=1`, never on by default.

## Skills to load

No dedicated Python knowledge-base skill exists yet (planned, not built; see
`~/dev/knowledge-base/INDEX.md`). Until then, verify FastMCP/Starlette/pydantic APIs against
current docs when unsure rather than writing from memory, same principle as the Swift skills
elsewhere in this stack.
