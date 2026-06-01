# Chat Interface — Specification

## Overview

A lightweight chat web app co-hosted on the Whatbox slot alongside the existing MCP server. Mum authenticates via her Plex account, then chats with Claude Haiku which is pre-wired to the running MCP server as a tool-calling backend. Exposed via a whatbox SSL link.

---

## Repository layout

```
src/whatbox_media_mcp/
  chat/
    __init__.py
    server.py        # FastAPI app + entry point (main())
    auth.py          # Plex PIN OAuth flow + session management
    config.py        # ChatSettings (extends / reuses Settings)
    mcp_client.py    # Thin wrapper around fastmcp.Client
    ai.py            # Haiku conversation loop (tool use, dry-run handling)
    static/
      index.html     # Single-page chat UI (vanilla JS, no build step)
```

`just run-chat` → `uv run python -m whatbox_media_mcp.chat.server`
`just test-chat` → `uv run pytest tests/chat`

---

## New dependencies

- `anthropic>=0.25` — Claude Haiku API
- `itsdangerous>=2` — signed session cookies

All other dependencies (`fastmcp`, `httpx`, `starlette`, `uvicorn`, `plexapi`, `pydantic-settings`) are already present.

---

## Configuration

`ChatSettings(Settings)` in `chat/config.py` — reads the same `.env` file, inherits all existing fields, adds:

| Env var | Default | Notes |
|---|---|---|
| `CHAT_HOST` | `127.0.0.1` | bind address |
| `CHAT_PORT` | `17433` | listen port |
| `CHAT_PUBLIC_BASE_URL` | required | e.g. `https://example.whatbox.ca/chat` — used as Plex callback origin |
| `CHAT_SESSION_SECRET` | required | random string for signing cookies |
| `CHAT_PLEX_CLIENT_ID` | required | stable UUID identifying this app to Plex |
| `ANTHROPIC_API_KEY` | required | Haiku API key |
| `SYSTEM_PROMPT_PATH` | `None` | path to override system prompt file |

`mcp_host`, `mcp_port`, and `mcp_bearer_token` are inherited from `Settings` — the chat server derives the MCP URL as `http://{mcp_host}:{mcp_port}/mcp`.

---

## Plex authentication

Uses Plex's PIN-based OAuth. No Plex credentials are stored; the session only records the verified Plex username.

### Flow

1. **`GET /auth/login`** — backend calls `POST https://plex.tv/api/v2/pins` (with `X-Plex-Client-Identifier` and `X-Plex-Product` headers) to obtain `{id, code}`. Stores `pin_id` in a short-lived signed cookie, redirects browser to:
   ```
   https://app.plex.tv/auth#?clientID={client_id}&code={code}&forwardUrl={CHAT_PUBLIC_BASE_URL}/auth/callback
   ```

2. **`GET /auth/callback`** — Plex redirects here after the user authenticates. Backend reads `pin_id` from the signed cookie, polls `GET https://plex.tv/api/v2/pins/{pin_id}` (up to ~10 retries, 1 s apart) until `authToken` is populated. On timeout → redirect to `/auth/login?error=timeout`.

3. **Server verification** — mum is a friend/shared user (full Plex.tv account, invited via the Plex UI). These do not appear in `server.systemAccounts()` (which only covers local/managed home users). Correct approach: create `MyPlexAccount(token=admin_plex_token)` using the existing `plex_token` from settings, call `.users()` to get all friends with server access, then create `MyPlexAccount(token=user_auth_token)` to get the authenticated user's `username`. If the username matches any friend → accept. Otherwise → redirect to `/auth/login?error=unauthorized`.

4. **Session cookie** — set a permanent signed cookie (`max_age` unset, browser-persistent) containing `{"plex_username": "..."}`. No server-side session store needed; the cookie is verified by signature on each request.

### Session middleware

A Starlette middleware reads the session cookie on every request to `/chat*` and `/api/*`. Unauthenticated requests redirect to `/auth/login`. The `/auth/*` routes are exempt.

---

## MCP client

`chat/mcp_client.py` wraps `fastmcp.Client`:

```python
from fastmcp import Client

def make_client(settings: ChatSettings) -> Client:
    mcp_url = f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
    return Client(mcp_url, auth={"Authorization": f"Bearer {settings.mcp_bearer_token.get_secret_value()}"})
```

The client is created once at app startup and stored on app state. Each chat turn opens a context-managed session: `async with client:` for the duration of the tool-calling loop. This is lightweight and avoids stale connection issues.

---

## AI conversation loop (`chat/ai.py`)

Uses `anthropic.AsyncAnthropic`. Standard multi-turn tool-use loop — no streaming.

### Model
`claude-haiku-4-5-20251001`

### Tool list
All tools exposed by the MCP server are fetched at startup via `client.list_tools()` and converted to Anthropic tool dicts. No filtering — the system prompt handles behaviour.

### Dry-run / confirmation flow

Every mutating tool (`radarr_add_movie`, `radarr_delete_movie`, `sonarr_add_series`, `sonarr_delete_series`, and all `*_queue_action` / `*_research_*` variants) has a `confirm: bool = False` parameter. When called without `confirm=True`, the tool returns a preview (`{"dry_run": true, "would_add": ...}`) without side effects.

System prompt instructs Haiku:
- For any mutating tool, always call with `confirm=False` first.
- When the tool response contains `"dry_run": true`, present the preview to the user in clear plain language and ask them to confirm before proceeding.
- Only call the same tool again with `confirm=True` after the user explicitly says yes.
- If the user declines, acknowledge and do nothing further.

This means no special backend state machine is needed — the dry-run result comes back to Haiku as a normal tool response, Haiku composes the confirmation question naturally, and the conversation continues.

### Loop behaviour
- Tool calls are executed by forwarding to the MCP server via the `fastmcp.Client`.
- The loop continues until Haiku returns a `stop_reason` of `end_turn` (no pending tool calls).
- Full message history is held in memory for the duration of the browser session (stored in the signed cookie or server-side dict keyed by session ID — see below).

### Conversation history
Stored server-side in an in-memory dict keyed by a session ID embedded in the session cookie. No persistence across server restarts (non-requirement). Each session gets a new conversation on server restart. Memory is not bounded — Haiku's context window is the practical limit.

---

## System prompt

Default hardcoded in `chat/ai.py`. Loaded from `SYSTEM_PROMPT_PATH` if set.

Draft (to be tuned):

> You are a friendly media assistant for a personal Plex server. You help the user find out what's in the library, what's downloading, and manage their media collection. You have access to tools for Plex, Radarr, and Sonarr.
>
> Rules:
> - Be warm, concise, and plain-spoken. Avoid technical jargon unless asked.
> - For any action that adds or removes media, always call the tool with `confirm=False` first to get a preview. Present the preview clearly and ask the user to confirm before proceeding.
> - Only call the tool again with `confirm=True` after the user explicitly says yes.
> - You do not have access to the internet. If asked about things outside the media library, politely say you can only help with media on this server.

---

## HTTP API

Served by a single Starlette/FastAPI app in `chat/server.py`.

| Route | Description |
|---|---|
| `GET /` | Redirect to `/chat` |
| `GET /chat` | Serve `static/index.html` |
| `GET /auth/login` | Start Plex PIN flow |
| `GET /auth/callback` | Plex redirect target; completes auth and sets session cookie |
| `POST /api/chat` | Accept `{"message": str, "history": [...]}`, run AI loop, return `{"reply": str, "history": [...]}` |
| `POST /api/logout` | Clear session cookie, redirect to `/auth/login` |

`/api/chat` requires authentication. `history` is the full message array echoed back and forth between client and server — no server-side storage required for history (avoids memory management complexity, at the cost of slightly larger request payloads).

---

## Frontend (`static/index.html`)

Single self-contained HTML file. Vanilla JS, no build step, no framework.

### Layout
- Fixed-height chat bubble list, scrolls to bottom on new message.
- Input bar pinned to bottom with send button.
- Bouncing ellipsis shown while waiting for API response.
- Logout link in corner.

### Confirmation UX
When Haiku's reply asks for confirmation (natural language), it renders as a normal chat bubble. No special detection or UI widget needed — the system prompt ensures Haiku phrases confirmations clearly. A subtle visual distinction (e.g. slightly different bubble colour or a ⚠ prefix) can be applied if Haiku is instructed to prefix confirmation messages with a sentinel like `[confirm]` — decision deferred to implementation.

### Auth redirect
If `/api/chat` returns 401, the page redirects to `/auth/login`.

---

## Deployment on Whatbox

Two processes, same venv:

```bash
# Process 1 — existing
whatbox-media-mcp  # listens on 127.0.0.1:17432

# Process 2 — new
whatbox-chat       # listens on 127.0.0.1:17433
```

Nginx config (added to existing whatbox SSL vhost):

```nginx
location /chat {
    proxy_pass http://127.0.0.1:17433;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
}
```

`CHAT_PUBLIC_BASE_URL=https://{slot}.whatbox.ca/chat`

Both processes managed via `screen`/`tmux` sessions or systemd user units — existing operational pattern for the MCP server applies unchanged.

---

## Testing

Follows existing project conventions: `pytest-asyncio`, `respx` for HTTP mocking, `@pytest.mark.live` gated by `LIVE_TESTS=1`.

### `just test-chat` — unit tests (`tests/chat/`)

**`test_auth.py`**
- PIN flow constructs correct Plex redirect URL (client ID, code, forwardUrl)
- Callback polls until `authToken` populated → sets signed session cookie
- Callback exhausts retries without `authToken` → redirects to `/auth/login?error=timeout`
- Server verification: mock `MyPlexAccount(admin_token).users()` returning a list; user's username in list → accepted
- Server verification: user's username not in list → redirects to `/auth/login?error=unauthorized`
- Session middleware: unauthenticated request to `/api/chat` → 302 to `/auth/login`
- Session middleware: requests to `/auth/*` are exempt from the check

**`test_ai.py`**
- System prompt: returns hardcoded default when `SYSTEM_PROMPT_PATH` unset
- System prompt: loads from file when `SYSTEM_PROMPT_PATH` points to a temp file
- Conversation loop: mock Anthropic client returns `end_turn` immediately → reply returned, no tool calls made
- Conversation loop: mock Anthropic client returns `tool_use` → tool forwarded to mock MCP client → result injected into messages → loop continues until `end_turn`

**`test_config.py`**
- `ChatSettings` loads with all required fields supplied
- MCP URL correctly derived as `http://{mcp_host}:{mcp_port}/mcp` from inherited settings

### `just test-chat-live` — live tests (`tests/chat/`, `@pytest.mark.live`)

Requires `LIVE_TESTS=1` and `ANTHROPIC_API_KEY` set in `.env` (same pattern as existing `just test-live`). These hit the real Anthropic API and the running MCP server.

1. **No-tool turn**: send `"hello"` — verify non-empty text response and zero MCP tool calls made
2. **Read-tool turn**: send `"what's currently downloading?"` — verify at least one MCP tool was called and response is non-empty text
3. **Dry-run turn**: send `"add the movie Paddington (2014)"` — verify Haiku called a tool with `confirm=False` (i.e. tool result contains `"dry_run": true`) and response contains confirmation language before any `confirm=True` call is made

---

## What is explicitly out of scope

- Saved conversations / chat history across sessions
- Internet / web search
- Fine-grained tool permissions or per-user access control
- Streaming responses
- Any UI beyond a functional chat page
