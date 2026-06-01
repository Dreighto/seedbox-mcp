# Whatbox Media Steward

Two co-hosted services for a Whatbox media stack:

- **MCP server** (`whatbox-media-mcp`) — exposes compact tools for Plex, Radarr, and Sonarr so your agent of choice can inspect media state and do basic actions.
- **Chat interface** (`whatbox-chat`) — a Plex-authenticated single-page chat UI powered by Claude Haiku, pre-wired to the MCP server. Designed for family members who have Plex access but not Claude.

---

## MCP server

The service is intentionally narrow:

- Plex is read-only.
- Radarr and Sonarr writes are limited to add, re-search, queue actions, and exact-ID delete.
- Write tools dry-run by default with `confirm=false`.
- Delete tools preserve files by default with `delete_files=false`.
- There are no torrent, indexer, filesystem, shell, scheduler, database, or frontend capabilities.

## Local Setup

Requires `just` and `uv`.

```bash
cp .env.example .env
# fill in required values — see Configuration below
just setup
just run        # MCP server on :17432
just run-chat   # chat interface on :17433 (separate terminal)
```

The MCP endpoint is `/mcp`; a basic unauthenticated health check is available at `/health`.

Every `/mcp` request requires either a static bearer token or a valid OAuth access token:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
```

## Configuration

All settings are read from `.env` or environment variables.

### Required

| Variable | Notes |
|---|---|
| `MCP_BEARER_TOKEN` | Shared secret for bearer auth and the OAuth consent gate |
| `MCP_PUBLIC_BASE_URL` | Public HTTPS base URL (e.g. `https://mcp.example.box.ca`) — required for OAuth discovery metadata |
| `RADARR_URL` | Radarr base URL |
| `RADARR_API_KEY` | Radarr → Settings → General → Security |
| `RADARR_DEFAULT_ROOT_FOLDER` | Absolute path to movies root folder on the slot |
| `RADARR_DEFAULT_QUALITY_PROFILE_ID` | Integer — see [Finding quality profile IDs](#finding-quality-profile-ids) |
| `SONARR_URL` | Sonarr base URL |
| `SONARR_API_KEY` | Sonarr → Settings → General → Security |
| `SONARR_DEFAULT_ROOT_FOLDER` | Absolute path to TV root folder on the slot |
| `SONARR_DEFAULT_QUALITY_PROFILE_ID` | Integer — see [Finding quality profile IDs](#finding-quality-profile-ids) |
| `PLEX_URL` | Plex base URL — use the public HTTPS address on Whatbox (not `127.0.0.1:32400`) |
| `PLEX_TOKEN` | Plex auth token — see [Finding your Plex token](#finding-your-plex-token) |

### Optional

| Variable | Default | Notes |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` | Bind address |
| `MCP_PORT` | `17432` | Bind port |
| `RADARR_DEFAULT_MIN_AVAILABILITY` | `released` | `announced`, `in_cinemas`, or `released` |
| `SONARR_DEFAULT_LANGUAGE_PROFILE_ID` | _(none)_ | Sonarr v3 language profile ID; omit for Sonarr v4+ |
| `SONARR_DEFAULT_SERIES_TYPE` | `standard` | `standard`, `daily`, or `anime` |
| `PLEX_MOVIE_SECTION` | `Movies` | Display name of the Plex movie library |
| `PLEX_TV_SECTION` | `TV Shows` | Display name of the Plex TV library |
| `TAUTULLI_ENABLED` | `false` | Set `true` to enable Tautulli watch-history enrichment |
| `TAUTULLI_URL` | _(none)_ | Tautulli base URL |
| `TAUTULLI_API_KEY` | _(none)_ | Tautulli API key |
| `OAUTH_ACCESS_TOKEN_TTL` | `3600` | OAuth access token lifetime in seconds |

Startup logs print a redacted config summary. API keys, Plex tokens, bearer tokens, and request headers are never logged.

---

## Chat Interface

A single-page chat UI backed by Claude Haiku. Anyone who has friend-level access to the Plex server can log in with their Plex account and chat with an assistant that has full access to the MCP tools.

Write actions (add, delete, queue) require an in-chat confirmation step — Haiku always calls tools with `confirm=false` first to show a preview, and only proceeds with `confirm=true` after the user says yes.

### Configuration

The chat server reads the same `.env` file as the MCP server and adds:

#### Required

| Variable | Notes |
|---|---|
| `CHAT_PUBLIC_BASE_URL` | Public HTTPS base URL for the chat app (e.g. `https://example.whatbox.ca/chat`) — used as the Plex OAuth callback origin |
| `CHAT_SESSION_SECRET` | Long random string used to sign session cookies |
| `CHAT_PLEX_CLIENT_ID` | Stable UUID identifying this app to Plex — generate once with `python3 -c "import uuid; print(uuid.uuid4())"` |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Haiku |

#### Optional

| Variable | Default | Notes |
|---|---|---|
| `CHAT_HOST` | `127.0.0.1` | Bind address |
| `CHAT_PORT` | `17433` | Bind port |
| `SYSTEM_PROMPT_PATH` | _(none)_ | Path to a plain-text file that overrides the default system prompt |

### How authentication works

1. User visits the chat URL and is redirected to `plex.tv` to sign in.
2. After signing in, Plex redirects back to `/auth/callback`.
3. The server verifies the user is a friend (shared-access user) of the Plex server.
4. A signed session cookie is set — no expiry, valid until the browser clears cookies.

Only users who already have Plex friend access can log in. There is no separate user management.

### Whatbox deployment

Run alongside the MCP server. Each needs its own `screen` session:

```bash
screen -dmS media-mcp  bash -c 'cd ~/seedboxmcp && just run      2>&1 | tee -a ~/seedboxmcp/mcp.log'
screen -dmS media-chat bash -c 'cd ~/seedboxmcp && just run-chat  2>&1 | tee -a ~/seedboxmcp/chat.log'
```

Add both to crontab for reboot persistence:

```cron
@reboot sleep 30 && screen -dmS media-mcp  bash -c 'cd ~/seedboxmcp && just run      2>&1 | tee -a ~/seedboxmcp/mcp.log'
@reboot sleep 30 && screen -dmS media-chat bash -c 'cd ~/seedboxmcp && just run-chat  2>&1 | tee -a ~/seedboxmcp/chat.log'
```

Configure a second Whatbox managed HTTPS link forwarding to `http://127.0.0.1:17433` and set `CHAT_PUBLIC_BASE_URL` to the resulting URL.

### Finding your Plex token

Open Plex Web, play any item, then choose "Get Info" → "View XML". The `X-Plex-Token` query parameter in the URL is your token. Full guide: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

### Finding quality profile IDs

The integer ID is visible in the URL when you click Edit on a profile in Settings → Profiles (e.g. `/settings/profiles/edit/2` → ID is `2`). You can also query the API:

```bash
# Radarr
curl -s "https://<radarr-host>/api/v3/qualityprofile?apikey=<key>" | python3 -m json.tool

# Sonarr
curl -s "https://<sonarr-host>/api/v3/qualityprofile?apikey=<key>" | python3 -m json.tool
```

## Whatbox Deployment

### Clone and install

```bash
git clone <repo-url> ~/seedboxmcp
cd ~/seedboxmcp
cp .env.example .env
# fill in all required values
uv sync
```

### Run in the background with screen

```bash
screen -dmS media-mcp bash -c 'cd ~/seedboxmcp && scripts/run.sh 2>&1 | tee -a ~/seedboxmcp/mcp.log'
```

Reattach: `screen -r media-mcp`. Detach: `Ctrl-A D`.

### Auto-restart on slot reboot

```bash
crontab -e
```

Add:

```cron
@reboot sleep 30 && screen -dmS media-mcp bash -c 'cd ~/seedboxmcp && scripts/run.sh 2>&1 | tee -a ~/seedboxmcp/mcp.log'
```

The `sleep 30` gives the slot time to bring up networking before the server tries to connect.

### Restart the server

```bash
screen -S media-mcp -X quit
screen -dmS media-mcp bash -c 'cd ~/seedboxmcp && scripts/run.sh 2>&1 | tee -a ~/seedboxmcp/mcp.log'
```

### Whatbox managed HTTPS link

Configure a Whatbox managed HTTPS link forwarding to `http://127.0.0.1:17432` and set `MCP_PUBLIC_BASE_URL` to the resulting HTTPS URL.

See the [Chat Interface](#chat-interface) section for deploying the companion chat server.

### Verify

```bash
curl https://<your-host>/health
curl https://<your-host>/.well-known/oauth-authorization-server
```

## Connecting to Agents

Both Claude.ai and ChatGPT MCP connectors require OAuth 2.0. The server implements Authorization Code + PKCE automatically — no extra setup beyond a running server with `MCP_PUBLIC_BASE_URL` set.

### Setup steps

1. In the agent of your choice, add a new MCP integration:
   - **MCP Server URL:** `https://<your-host>/mcp`
   - **Client ID:** any string, e.g. `claude-ai` (the server accepts any value here)
   - **Client Secret:** leave blank or enter any dummy value — it is not validated (PKCE replaces client secrets)
2. The agent will open `https://<your-host>/oauth/authorize` in your browser.
3. Paste your `MCP_BEARER_TOKEN` into the consent form and submit.
4. The agent stores the resulting access token (default 1-hour TTL) and refreshes it automatically via a 30-day refresh token.

### Suggested model-facing instructions

```text
Prefer read-only tools first. For add, delete, re-search, and queue operations, first identify the exact item with media_search or an overview tool. For ambiguous titles, return candidates or ask for disambiguation. Do not invent IDs. Queue actions require the queue_id from radarr_overview or sonarr_overview output. Do not claim to access torrent clients, indexers, shell, or the filesystem; this app intentionally does not expose those capabilities.
```

## Command Interface

Consult the `justfile`:

```bash
just setup            # install runtime and dev dependencies with uv
just run              # run the MCP server (:17432)
just run-chat         # run the chat interface (:17433)
just test             # run MCP unit tests (no live services required)
just test-chat        # run chat unit tests (no live services required)
just test-live        # run MCP live integration tests (requires LIVE_TESTS=1)
just test-chat-live   # run chat live tests (requires LIVE_TESTS=1 + running MCP server)
just test-smoke       # call /health on the configured host/port
just format           # format with ruff
just check            # ruff lint + mypy type-check
```

## Tool Surface

Read-only tools:

- `media_status` — Radarr, Sonarr, and Plex reachability, version, health, and disk space
- `radarr_overview` — movie list, download queue, and wanted/missing
- `sonarr_overview` — series list, download queue, and wanted/missing episodes
- `plex_overview` — active sessions, recently added, and unwatched staleness candidates
- `media_search` — fuzzy search across Radarr, Sonarr, and Plex with external TMDb/TVDb lookup
- `staleness_report` — cross-references Plex watch history against Radarr/Sonarr to surface old unwatched, unmanaged, and queue-stuck items

Write tools:

- `radarr_add_movie` — add a movie by TMDb ID (dry-run by default)
- `radarr_research_movie` — trigger a search, refresh, or downloaded-scan command on a Radarr movie
- `radarr_delete_movie` — remove a movie from Radarr by internal ID (preserves files by default)
- `radarr_queue_action` — `remove` or `blocklist` a stuck queue item by queue ID
- `sonarr_add_series` — add a series by TVDb ID (dry-run by default)
- `sonarr_research_series` — trigger a series search, refresh, or missing-episode search
- `sonarr_delete_series` — remove a series from Sonarr by internal ID (preserves files by default)
- `sonarr_queue_action` — `remove` or `blocklist` a stuck queue item by queue ID

Write tools require explicit identifiers. Add operations require TMDb or TVDb IDs. Delete, re-search, and queue operations require exact Radarr or Sonarr internal IDs. Queue actions never touch the torrent client (`removeFromClient=false`).

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

```text
Are there any stuck or import-blocked items in the Radarr or Sonarr queue? If so, clear them.
```

## Troubleshooting

- **Missing config:** run `cp .env.example .env`, then fill in all required values.
- **401 from `/mcp`:** verify `Authorization: Bearer <MCP_BEARER_TOKEN>`.
- **OAuth consent form rejects your token:** paste the exact value of `MCP_BEARER_TOKEN` from your `.env` with no extra whitespace.
- **Claude.ai "Authorization failed" after consent:** check that `MCP_PUBLIC_BASE_URL` is set and matches your public hostname; verify with `curl https://<host>/.well-known/oauth-authorization-server` that `token_endpoint` is a full HTTPS URL.
- **Radarr or Sonarr auth errors:** verify API keys in each app.
- **Plex errors:** use the public HTTPS Whatbox URL for `PLEX_URL`, not `127.0.0.1:32400`. Verify `PLEX_TOKEN` and section names (`PLEX_MOVIE_SECTION`, `PLEX_TV_SECTION`).
- **Partial status warnings:** one upstream may be down while the MCP server itself is healthy.

Automated tests (`just test`) use mocked upstreams and do not require live services. Use `just test-live` to test access to external services.
