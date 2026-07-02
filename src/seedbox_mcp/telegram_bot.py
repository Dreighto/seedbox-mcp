from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client

from seedbox_mcp.chat.ollama_ai import (
    ACTION_TOOLS,
    DEFAULT_OLLAMA_URL,
    ESCALATION_TOOLS,
    READ_ONLY_TOOLS,
    run_agent_turn,
    trim_history,
)
from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import TELEGRAM_API, send_message

logger = logging.getLogger("seedbox_mcp.telegram_bot")

# Interactive replies favor a smaller/faster cloud model over the digest's
# big batch model — a few seconds of turnaround reads fine in a chat, and the
# ollama_ai harness now survives a bad tool call from a weaker model instead
# of crashing (see chat/ollama_ai.py — this is exactly the model that
# originally exposed that bug).
DEFAULT_BOT_MODEL = "gpt-oss:20b-cloud"

# Poster/cover identification specifically needs more reliable multi-step
# reasoning (reconstruct garbled OCR fragments, verify with a follow-up
# search, disambiguate same-titled entries) than a quick text reply does —
# live testing found DEFAULT_BOT_MODEL genuinely inconsistent on this one
# task across three back-to-back runs against the same real photo: one
# clean correct identification, one that produced unrelated guesses and
# leaked a malformed reply, one that never finished within a generous
# timeout. Same tradeoff logic as monitor.py's model choice — this path
# isn't latency-sensitive enough to justify the smaller model's
# unreliability here.
PHOTO_IDENTIFY_MODEL = "qwen3-coder:480b-cloud"
PHOTO_IDENTIFY_MAX_TOOL_ROUNDS = 12
POLL_TIMEOUT_S = 30

# Conversation history, persisted to disk — this service gets restarted
# often during active development, and losing multi-turn context on every
# redeploy defeats the point of adding it. Keyed by chat_id even though only
# one chat is ever allowed today, so this doesn't need reshaping if that
# changes. Plain JSON, not a database — this is a handful of KB for one
# operator's conversation, not a scaling concern.
#
# Per-chat value is {"history": [...], "pending_action": {...} | null,
# "known_entity_ids": {...}} — pending_action and known_entity_ids travel
# alongside history because they're the same kind of per-conversation state
# (see ollama_ai.run_agent_turn's params of the same names): the single
# action-tool preview currently confirmable, and the set of entity ids
# (tmdb_id, tvdb_id, ...) actually observed from real tool results so a
# later write can't reference an invented one. Losing either on restart just
# means an in-flight confirm/id-reference fails closed and needs a fresh
# preview/lookup — an ok-either-way default rather than a real problem.
HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / ".telegram_bot_history.json"


class ChatState(dict[str, Any]):
    """{"history": list[dict], "pending_action": dict | None,
    "known_entity_ids": dict[str, list[int]]} — a thin alias so callers
    don't have to spell out the shape every time."""


def _load_chat_states() -> dict[int, ChatState]:
    if not HISTORY_PATH.exists():
        return {}
    try:
        raw = json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.exception("failed to load %s, starting with empty history", HISTORY_PATH)
        return {}
    states: dict[int, ChatState] = {}
    for chat_id, value in raw.items():
        try:
            key = int(chat_id)
        except ValueError:
            continue
        if isinstance(value, list):
            # Pre-pending-action-gate format — a bare history list. Treat as
            # no pending action / no known ids rather than guessing from old data.
            states[key] = ChatState(history=value, pending_action=None, known_entity_ids={})
        elif isinstance(value, dict):
            states[key] = ChatState(
                history=value.get("history", []),
                pending_action=value.get("pending_action"),
                known_entity_ids=value.get("known_entity_ids", {}),
                active_sections=value.get("active_sections", []),
            )
    return states


def _save_chat_states(states: dict[int, ChatState]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps({str(k): v for k, v in states.items()}))
    except OSError:
        logger.exception("failed to persist history to %s", HISTORY_PATH)


# ── Sectioned system prompt + tool subsetting ────────────────────────────
# The measured per-turn payload before this existed was ~12k tokens on
# EVERY message (a ~2,970-token monolithic prompt + ~9,100 tokens of all
# 52 tool schemas), sent even for "what's the queue status". Tool schemas
# were 3x the prompt, so the tool subset matters more than the prompt trim.
# Design (from the context-bloat research workflow, 2026-07-01): an
# always-on core plus keyword-gated sections, each bundling its prompt
# text AND its tools. Plain substring matching, no extra LLM call, fully
# deterministic. Sections are STICKY per conversation (stored in
# ChatState.active_sections): once a topic activates, its tools stay
# loaded for follow-ups like "yes do it" that carry no keywords — without
# stickiness, the confirm turn of a two-step flow would lose the very
# tool it needs to confirm with.

PROMPT_CORE = """\
You are the operator's NAS Ops assistant, talking to them directly over \
Telegram. You have tools covering the whole NAS: Plex, Radarr, Sonarr, \
downloads, requests, backups, and storage. Only some tools are loaded per \
conversation, matched to the topic — if the operator asks for something \
you have no tool for right now, say you're not set up for that in this \
conversation and suggest they re-ask in a fresh message naming the thing \
directly (that reloads the right tools); don't improvise with the wrong \
tool.

For anything about downloads, requests, or "is X available" — prefer the \
nasdoom_* tools (the NASDOOM app's own BFF) over the raw service tools:
- nasdoom_omni_search(query) — the right tool for "do we have X" / "can I \
get X" about one title. Already resolves inLibrary/managed/acquirable.
- nasdoom_queue — unified SABnzbd + arr-import download queue.
- nasdoom_requests_overview — friend-request state with plain-English labels.
- nasdoom_health / nasdoom_control — quick reachability and storage checks.

You can also act, not just look things up — action tools for reversible, \
low-stakes changes: nasdoom_queue_command / nasdoom_queue_item_command \
(pause/resume/cancel/reprioritize a download), nasdoom_requests_action \
(approve/decline a request), nasdoom_match_search + nasdoom_match_apply \
(fix a mismatched Plex item). Each takes confirm=false|true — ALWAYS call \
with confirm=false first, it returns the current state and exactly what \
would change without writing anything. If that preview looks right, call \
again with confirm=true right away, in direct response to the operator \
actually confirming that specific thing. Use these when the operator asks \
directly ("pause the queue") or when it's the obvious next step in the \
conversation — you don't need to ask the operator's permission for \
something this reversible (the confirm=false preview step already covers \
that), but say plainly what you did. If the preview shows something \
unexpected (wrong item matched, found=false), stop and ask the operator \
instead of forcing confirm=true.

Important: confirm=true only works if it matches a preview you just ran — \
the harness rejects it otherwise (error_type "not_permitted"), so don't \
invent a confirm=true call because a message sounds like agreement ("yes", \
"go ahead", "sure") when you haven't actually proposed anything specific to \
confirm, and don't treat an unrelated question as a reason to fire one \
off. If a "yes" doesn't clearly map to one specific thing you just \
previewed, ask what it's confirming instead of guessing. If you get the \
not_permitted rejection, that means there's nothing live to confirm — tell \
the operator, don't retry blindly.

Never invent a specific value for an action parameter (a speedcap percentage, \
a priority level, anything numeric or named) that the operator never actually \
stated anywhere in the conversation — including when you're the one who just \
asked "what level would you like?" and they replied with a bare "yes" or "go \
ahead" instead of an actual number. If the operator's reply doesn't \
contain the actual value your own question was asking for, ask again instead \
of picking one — don't treat generic agreement as license to fill in a \
specific number yourself.

For anything broken that's outside these tools — a failed backup, a service \
that's down, config drift — call escalate_to_worker with a clear \
description, tell the operator you escalated it and why, then move on. \
Don't try to fix system-level things yourself. This includes when \
the operator tells you something is broken but your own read-only check \
says it's fine — say the discrepancy out loud and escalate it for a closer \
look rather than resolving the conflict yourself. Never hand the operator \
raw commands to run themselves (journalctl, systemctl, anything \
shell-level) as your answer, and never suggest a manual, ungated path \
(deleting files directly, editing config by hand) even as an aside — if \
there's no tool-gated way to do something, say so plainly and escalate or \
leave it to the operator.

This is a live conversation with the one person who runs this NAS, not a \
scheduled report — answer their actual question, don't pad it into a \
digest-style report unless they asked for one. Be direct and concise; a \
one-line answer to a one-line question is correct. Use tools whenever the \
answer depends on live state — don't guess.

Formatting: this renders in Telegram, not a markdown viewer. It doesn't \
support tables in any mode, and only single *asterisks* make bold text \
(double **asterisks** show up as literal asterisks). Never use a markdown \
table; use short "Label: value" lines instead. Keep formatting light.

Writing style:
- No em-dashes. Use a period, comma, or semicolon instead.
- No filler ("it's important to note") and no hedging ("may potentially"). \
Say the thing or don't say it.
- No intensifiers standing in for a number ("significantly faster") — give \
the actual figure, or drop the claim.
- Before presenting two things as separate options, check they're actually \
different (year, edition, id). If nothing distinguishes them, it's the \
same result found twice; present it once. Don't restate the same fact two \
ways in one reply.
"""

# Tools always loaded, matched to what PROMPT_CORE describes: status,
# queue/request actions, match-fix, escalation. Everything else rides in
# a section.
CORE_TOOLS: set[str] = {
    "media_status",
    "plex_overview",
    "plex_library_size",
    "nasdoom_health",
    "nasdoom_queue",
    "nasdoom_omni_search",
    "nasdoom_requests_overview",
    "nasdoom_control",
    "nasdoom_queue_command",
    "nasdoom_queue_item_command",
    "nasdoom_requests_action",
    "nasdoom_match_search",
    "nasdoom_match_apply",
    "nas_backup_health",
    "nas_storage_inventory",
    "escalate_to_worker",
}

# Each section: trigger keywords (case-insensitive substring match against
# the operator's message), prompt text appended to PROMPT_CORE, and the
# tool names loaded alongside. Over-matching is safe (a few extra schemas);
# under-matching is the failure mode — keep keyword lists generous, and
# remember the core prompt tells the model what to say when a capability
# genuinely isn't loaded.
PROMPT_SECTIONS: dict[str, dict[str, Any]] = {
    "library": {
        "keywords": [
            "add", "get ", "grab", "download", "movie", "film", "show", "series", "season",
            "episode", "anime", "quality", "profile", "blocklist", "block list", "calendar",
            "releas", "airing", "upcoming", "monitor", "search", "rescan", "refresh",
            "metadata", "stuck", "failed", "radarr", "sonarr", "jellyseerr", "request",
            "stale", "unwatched", "haven't watched", "watch", "import", "won't finish",
            "wont finish", "permission", "not importing",
        ],
        "prompt": """\
You can manage the Radarr/Sonarr library directly, not just monitor it:
- media_search(query) or nasdoom_omni_search(query) — find a title's \
tmdb_id/tvdb_id first, ALWAYS, before calling nasdoom_add. The harness \
itself rejects an id that didn't come from a real search result here — \
never state or guess a tmdb_id/tvdb_id from your own memory; just search \
first.
- nasdoom_add — add a movie or series (kind: movie|tv). Leave \
quality_profile_id and root_folder_path unset unless the operator names a \
specific one — NASDOOM automatically routes anime to the anime \
folder/profile and everything else to the regular library default. Don't \
"upgrade" to a higher quality on your own judgment. search_now defaults to \
false (adds/monitors without an immediate grab) — only set true if asked \
to grab it now. nasdoom_profiles(kind) lists quality options if asked.
- radarr_research_movie / sonarr_research_series — fix something already in \
the library: search again, refresh metadata, or rescan a file on disk. Use \
this before ever considering a delete/re-add cycle.
- sonarr_monitor_season — "add season N of X" when the show's already in \
Sonarr but that season isn't monitored yet.
- radarr_queue_action / sonarr_queue_action — unstick a queue item at the \
arr level (remove or blocklist), distinct from nasdoom_queue_item_command; \
use whichever the operator's phrasing points at.
- nas_import_diagnosis — when something downloaded but WON'T IMPORT/finish, \
run this before blocklisting or re-grabbing. It pins the root cause \
(download-side permissions, library-side permissions, or a path/mount \
issue) by testing access as the arr's own user, and returns the exact \
chown fix for permission cases. That fix is a filesystem change on the NAS \
you do NOT run yourself — report the diagnosis and the command and offer \
to escalate it. Do not just blocklist an import failure; a re-download \
won't fix a permissions problem.
- radarr_calendar / sonarr_calendar — "what's releasing/airing soon".
- radarr_blocklist / sonarr_blocklist — see why something keeps failing to \
grab; radarr_blocklist_remove / sonarr_blocklist_remove un-blocks a \
release so it can be tried again (confirm-gated, reversible).
- staleness_report — library-wide sweeps ("what haven't I watched in 6 \
months"), not single-title lookups.
There is deliberately no delete tool in your reach — removing something \
from the library isn't part of what you can do yet.
""",
        "tools": {
            "media_search", "nasdoom_add", "nasdoom_profiles", "radarr_research_movie",
            "sonarr_research_series", "sonarr_monitor_season", "radarr_queue_action",
            "sonarr_queue_action", "radarr_calendar", "sonarr_calendar", "radarr_blocklist",
            "sonarr_blocklist", "radarr_blocklist_remove", "sonarr_blocklist_remove",
            "radarr_overview", "sonarr_overview", "staleness_report", "jellyseerr_overview",
            "nas_import_diagnosis",
        },
    },
    "web": {
        "keywords": [
            "web", "internet", "online", "look up", "lookup", "google", "error", "latest",
            "news", "best practice", "recommend", "better", "improve", "suggestion",
        ],
        "prompt": """\
For anything needing current outside information — an error message you \
don't recognize, current best practices, a new tool worth considering — \
use web_search (and web_fetch to read a specific result in full). Don't \
answer from possibly-stale training knowledge when a quick search would \
give a real answer. Proactive suggestions are fine — just be clear it's a \
suggestion; actually adding new infrastructure is escalate_to_worker \
territory.
""",
        "tools": {"web_search", "web_fetch"},
    },
    "find": {
        "keywords": ["sample", "music", "software", "game", "book", "kit", "prowlarr", "usenet", "find "],
        "prompt": """\
For non-video content — music samples/kits, software, games, books — use \
nasdoom_find(query, scope) to search, then nasdoom_find_grab(grab_id, ...) \
to download (same confirm=false preview-first pattern; double-check it's \
the right result from the find call before confirming). grab_ids expire in \
30 minutes — if a grab comes back expired, search again. share=true routes \
it into the shared/Transfer folder instead of the private library — only \
set that if the operator actually wants it shared.
""",
        "tools": {
            "nasdoom_find", "nasdoom_find_grab", "prowlarr_overview",
            "prowlarr_indexer_stats", "sabnzbd_overview",
        },
    },
    "share": {
        "keywords": ["share", "portal", "friend", "account", "upload", "revoke", "files.logueos"],
        "prompt": """\
For the friend file-share portal (files.logueos.xyz): \
nasdoom_share_friends_list and nasdoom_share_files_list are read-only. \
nasdoom_share_friend_create makes a real account with a real password \
(upload=false is download-only). nasdoom_share_friend_revoke removes \
access. Both take confirm=false|true — same preview-first pattern. \
Creating an account hands out real credentials, so read back the name \
before confirming if there's any ambiguity. There's no tool to delete a \
shared file — deliberate gap, the operator does that directly.
""",
        "tools": {
            "nasdoom_share_friends_list", "nasdoom_share_files_list",
            "nasdoom_share_friend_create", "nasdoom_share_friend_revoke",
        },
    },
    "speed": {
        "keywords": ["speed", "bandwidth", "connection", "slow", "fast"],
        "prompt": """\
For "what are my internet speeds" — use nas_internet_speed_test. It runs a \
real test FROM the NAS itself, takes ~5-10s, always fresh. It consumes \
real bandwidth (a few hundred MB), so don't call it more than once per \
conversation unless asked to re-test.
""",
        "tools": {"nas_internet_speed_test"},
    },
    "host": {
        "keywords": [
            "disk", "drive", "smart", "nvme", "ssd", "hdd", "hard drive", "dying", "failing",
            "restart", "container", "docker", "down", "unreachable", "crashed", "hung",
            "not responding", "offline", "service",
        ],
        "prompt": """\
Host-level diagnostics for the NAS box itself:
- nas_disk_health — SMART health for every physical drive. Verdicts \
(ok/watch/replace_now) are computed in code from real failure-rate \
thresholds; report them and their reasons EXACTLY as returned. Never \
soften a replace_now, never call a disk healthy on your own judgment, \
and never state an attribute value the tool didn't return.
- nas_service_status — the media-stack Docker containers' actual state. \
nasdoom_health checks the services' APIs; this checks the containers. \
When an API is down, check the container before assuming anything.
- nas_service_restart — restart one media container. These host tools \
are loaded whenever you can read this paragraph — never tell the \
operator you're "not set up" for a restart or that a new message would \
load the tool; that's false. When asked to restart something, run the \
confirm=false preview and let the tool's own allowlist decide: if it \
returns not_permitted, give the operator the REAL reason (that service \
is shared infrastructure, off the restart allowlist on purpose) and \
offer escalate_to_worker — don't invent a different explanation and \
don't retry with name variations. The preview shows the container's \
current state; show that to the operator and STOP — the harness only \
accepts confirm=true in a later turn, after they've replied, and it \
will reject a same-turn confirm. If the preview contradicts what the \
operator believed (they said it's down, preview says running), lead \
with that. After a confirmed restart, report the returned \
state_after/verified_running values; if verified_running is false, say \
the restart did NOT bring it back and escalate — never claim a service \
recovered without that flag.
A useful diagnostic chain when something's broken: nasdoom_health (API \
reachable?) → nas_service_status (container running?) → \
nas_service_restart if stopped/unhealthy → re-check nasdoom_health → \
escalate_to_worker if still broken, saying exactly what you tried.
""",
        "tools": {"nas_disk_health", "nas_service_status", "nas_service_restart"},
    },
    "stats": {
        "keywords": [
            "who watched", "history", "viewing", "stats", "tautulli", "watched", "user", "users",
            "account", "accounts", "who has access", "who's on", "whos on", "people", "member",
            "names", "watching", "streaming", "stream", "active",
        ],
        "prompt": """\
For the list of people/accounts on the server and viewing activity, use \
the Tautulli tools:
- tautulli_users — the actual Plex user roster (usernames, friendly \
names, emails, active flag). THIS is the tool for "how many users", \
"what are their names", "who has access". Report the real names it \
returns; never invent a count or names.
- plex_now_playing — who is watching RIGHT NOW, read straight from Plex \
(authoritative, not Tautulli): stream count, each viewer's name, what \
they're watching, local (LAN) vs remote (WAN), direct-play vs transcode, \
per-stream bandwidth, and bottleneck_flags computed in code (concurrent \
transcodes, throttled transcodes, remote streams, high total bandwidth). \
THIS is "how many people are watching" / "who's streaming" / "is the \
server struggling". Report bottleneck_flags verbatim when present.
- tautulli_history — what was watched, by whom, when.
- tautulli_user_stats — per-user viewing totals.
Do NOT answer a question about users or who's watching from \
jellyseerr_overview or nasdoom_requests_overview — those return REQUEST \
counts (how many titles have been requested), not people. A "total: 12" \
from a requests overview means 12 requested titles, not 12 viewers. Use \
tautulli_users for the account roster and plex_now_playing for live \
viewers. (Tautulli's own live activity is currently unreliable — it lost \
its Plex connection in the NAS migration — so live-viewer questions go to \
plex_now_playing, not Tautulli.)
""",
        "tools": {"tautulli_history", "tautulli_users", "plex_now_playing", "tautulli_user_stats"},
    },
}


def _select_sections(text: str, active: set[str]) -> set[str]:
    """Returns the updated sticky section set for this conversation —
    previously active sections stay, new keyword matches join."""
    lowered = text.lower()
    for name, section in PROMPT_SECTIONS.items():
        if name not in active and any(kw in lowered for kw in section["keywords"]):
            active.add(name)
    return active


def _build_context(active: set[str]) -> tuple[str, set[str]]:
    prompt_parts = [PROMPT_CORE]
    tools = set(CORE_TOOLS)
    for name in sorted(active):
        section = PROMPT_SECTIONS[name]
        prompt_parts.append(section["prompt"])
        tools |= section["tools"]
    return "\n".join(prompt_parts), tools

# Answered directly, without going through the model, when the message is
# exactly "/help" or "/start" (checked before run_agent_turn in
# _handle_message). Deterministic and instant: capability questions don't
# need a model call, and a model-generated capability list risks describing
# a tool that doesn't exist or omitting one that does. Keep this in sync by
# hand whenever READ_ONLY_TOOLS/ACTION_TOOLS gains or loses a tool — it's a
# curated summary for a human, not a generated dump of the tool schema.
HELP_TEXT = """\
I'm the NAS Ops assistant — ask me anything in plain English, no commands \
needed except this one. Here's what I can actually do:

📊 Status & health
Plex, Radarr, Sonarr, Prowlarr, SABnzbd, Jellyseerr reachability · backup \
health · NAS storage (Music/samples/Transfer) · a real internet speed test \
run from the NAS itself · Prowlarr per-indexer failure stats · library \
staleness sweeps ("what haven't I watched in months")

🎬 Library — Radarr/Sonarr
Search and check what's already tracked · add a movie or series (auto-\
routes anime vs regular content, uses your default quality unless you name \
one) · what's releasing/airing soon · fix a stuck item (re-search, refresh, \
rescan) · add a missing season · see and un-block a failed/blocklisted \
release · unstick a queue item · send a photo of a poster/cover and I'll \
OCR it and check if it's already in the library

📥 Downloads & requests
Unified download queue (pause/resume/speedcap/cancel/reprioritize) · \
approve or decline a pending Jellyseerr request · fix a mismatched Plex item

🎵 Everything else (music samples, software, games, books)
Search Prowlarr directly and download — the arrs don't cover this, this \
does

🌐 Web
Search the live web and read a page's full content — for anything outside \
this NAS's own state (error messages, current best practices, evaluating a \
new tool for the setup)

🔗 Friend file-share portal (files.logueos.xyz)
List/create/revoke friend accounts (download-only or upload-enabled) · see \
what's shared

Anything with real consequences (adding content, changing settings, \
downloading) always previews first and asks before doing it for real — I'll \
never just go do something without showing you what's about to happen.

Anything genuinely broken that I can't fix myself — a service down, a \
config problem — I hand off to a full worker with real system access and \
tell you I did it.

Not built yet, on purpose: deleting anything from the library, general \
storage cleanup. Those need a stronger safety pattern than what I have \
right now.
"""


async def _set_bot_commands(token: str) -> None:
    """Registers the /help command with Telegram so it shows in the client's
    command menu — best-effort, a failure here doesn't stop the bot from
    working, it just means the menu entry won't appear."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{TELEGRAM_API}/bot{token}/setMyCommands",
            json={"commands": [{"command": "help", "description": "What can you do?"}]},
        )
    if resp.is_error:
        logger.warning("setMyCommands failed (non-fatal): %s %s", resp.status_code, resp.text)


class BotSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_bot_model: str = DEFAULT_BOT_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def _handle_message(
    settings: BotSettings,
    token: str,
    chat_id: int,
    text: str,
    state: ChatState,
    model: str | None = None,
    max_tool_rounds: int | None = None,
    force_sections: set[str] | None = None,
) -> ChatState:
    """Runs one turn and returns the updated chat state — on error, returns
    `state` unchanged (a failed turn shouldn't inject partial/broken state
    into the next one). `model`/`max_tool_rounds` let a caller override the
    default fast/cheap interactive model for a specific task that needs
    more reliable multi-step reasoning (see _handle_photo_message).
    `force_sections` pre-activates prompt/tool sections regardless of
    keyword matching (the photo path needs library+web loaded even though
    the synthesized task string may not hit the keyword lists)."""
    active_sections = set(state.get("active_sections") or [])
    if force_sections:
        active_sections |= force_sections
    active_sections = _select_sections(text, active_sections)
    system_prompt, allowed_tools = _build_context(active_sections)
    logger.info("active sections: %s (%d tools)", sorted(active_sections) or ["core-only"], len(allowed_tools))

    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{TELEGRAM_API}/bot{token}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        run_kwargs: dict[str, Any] = {}
        if max_tool_rounds is not None:
            run_kwargs["max_tool_rounds"] = max_tool_rounds
        reply, new_history, new_pending_action, new_known_entity_ids = await run_agent_turn(
            text,
            system_prompt=system_prompt,
            mcp_client=mcp_client,
            model=model or settings.ollama_bot_model,
            allowed_tools=allowed_tools & (READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS),
            history=state.get("history", []),
            pending_action=state.get("pending_action"),
            known_entity_ids=state.get("known_entity_ids"),
            ollama_url=settings.ollama_url,
            **run_kwargs,
        )
        logger.info("reply: %r", reply)
    except Exception:
        logger.exception("agent turn failed for message: %r", text)
        await send_message(
            token,
            chat_id,
            "Something went wrong answering that. Check the service log (journalctl -u seedbox-telegram-bot).",
        )
        return state
    if not reply.strip():
        # Observed live: the model can end a turn with a tool call but no
        # follow-up text at all, especially on a harder multi-step task.
        # Telegram rejects an empty sendMessage outright, so without this
        # check the operator would get either nothing or a confusing error
        # instead of a clear signal that the turn didn't produce an answer.
        logger.warning("agent turn produced an empty reply for message: %r", text)
        await send_message(token, chat_id, "That didn't produce a clear answer. Could you try rephrasing?")
        return ChatState(
            history=trim_history(new_history),
            pending_action=new_pending_action,
            known_entity_ids=new_known_entity_ids,
            active_sections=sorted(active_sections),
        )
    await send_message(token, chat_id, reply)
    return ChatState(
        history=trim_history(new_history),
        pending_action=new_pending_action,
        known_entity_ids=new_known_entity_ids,
        active_sections=sorted(active_sections),
    )


async def _download_telegram_photo(token: str, file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=20.0) as http:
        resp = await http.get(f"{TELEGRAM_API}/bot{token}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        resp = await http.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        resp.raise_for_status()
        return resp.content


async def _handle_photo_message(
    settings: BotSettings, token: str, chat_id: int, photo_sizes: list[dict[str, Any]], caption: str, state: ChatState
) -> ChatState:
    """Downloads the photo, runs OCR deterministically (the model has no way
    to fetch Telegram file bytes itself — this is the same "do the
    mechanical part in code, hand the LLM only what needs judgment" pattern
    as monitor.py's queue-resume check), then routes the extracted text
    through the normal _handle_message pipeline so the model uses its usual
    search/add tools and confirm-gates on the result."""
    largest = max(photo_sizes, key=lambda p: p.get("file_size") or (p.get("width", 0) * p.get("height", 0)))
    try:
        image_bytes = await _download_telegram_photo(token, largest["file_id"])
    except httpx.HTTPError:
        logger.exception("failed to download photo from Telegram")
        await send_message(token, chat_id, "Couldn't download that photo from Telegram — try sending it again?")
        return state

    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    try:
        async with mcp_client:
            result = await mcp_client.call_tool("poster_ocr", {"image_b64": base64.b64encode(image_bytes).decode()})
        ocr_text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
        ocr_data = json.loads(ocr_text).get("data", {})
    except Exception:
        logger.exception("poster OCR failed")
        await send_message(token, chat_id, "Couldn't read text from that photo — the OCR service may be down.")
        return state

    texts = ocr_data.get("texts_by_prominence", [])
    logger.info("photo OCR result: %r", texts)
    if not texts:
        await send_message(
            token, chat_id, "Didn't find any readable text in that photo — is it a clear shot of the poster/cover?"
        )
        return state

    extracted = "; ".join(f'"{t["text"]}" (confidence {t.get("confidence", 0):.2f})' for t in texts[:8])
    # Deterministic signal, not left to the model's own judgment of "is this
    # thin" — if everything OCR caught is a single word, that's mechanically
    # too little to identify a specific title from, regardless of how the
    # model feels about it.
    all_single_words = all(len(str(t.get("text", "")).split()) <= 1 for t in texts)
    sparse_warning = (
        "\n\nWARNING: every extracted fragment is a single word. That is not enough to identify a specific "
        "title on its own, no matter how confident a match seems. Say so plainly rather than guessing."
        if all_single_words
        else ""
    )
    task = (
        f"The operator sent a photo. OCR extracted exactly this text, largest/most prominent first: {extracted}."
        f"{sparse_warning} "
        f"{'They also wrote: ' + caption if caption else ''} "
        "This is the COMPLETE list. There is no other text on the image beyond what's listed above, so don't "
        "describe or refer to any text that isn't in that list.\n\n"
        "Step 1 - attempt reconstruction. Poster typography (stylized logos, overlapping graphics) often defeats "
        "OCR on low-resolution images and produces garbled fragments that don't read as real words on their "
        "own. Before giving up on a garbled fragment, check whether it shares a letter prefix/suffix with a "
        "plausible word (e.g. 'BLADKSH' sharing 'BLAD' with 'BLADES'), and try combining ALL the fragments, "
        "garbled ones included, into one candidate title, the way a person squinting at a blurry poster would. "
        "A search of the raw combined fragments (messy, includes garbled words) is a reasonable first probe, "
        "but once you've settled on a clean candidate title in your own reasoning, run a SEPARATE follow-up "
        "search of just that clean title on its own. The clean search ranks results differently and can surface "
        "a better, more specific match than the messy one did; don't settle for whatever the first messy search "
        "happened to return if you have a cleaner guess to verify.\n\n"
        "Step 2 - verify the match actually explains everything. Only treat a search result as a real "
        "identification if it closely matches the FULL reconstruction, accounting for most or all of the "
        "extracted fragments, not just one. This is the critical check: a title that only explains ONE clean "
        "fragment while leaving the others (garbled or not) completely unaccounted for is NOT a match, it's a "
        "coincidence, no matter how well-known that title is. Real example of getting this wrong: OCR extracted "
        "'GUARDIANS' (clean) and 'BLADKSH' (garbled), and a reply confidently identified this as 'Guardians of "
        "the Galaxy' (a famous franchise that explains 'GUARDIANS' but has nothing to do with 'BLADKSH') "
        "instead of reconstructing 'Blades of the Guardians' (which explains both). Do not default to fame or "
        "familiarity when a candidate leaves fragments unexplained; a full reconstruction that fits everything "
        "beats a partial match to something famous.\n\n"
        "This same check applies just as hard when the title itself is an EXACT match, not just a partial one: "
        "if OCR also caught other credits (a director/cast name, production company, distributor, tagline, "
        "release date) alongside the title, those credits have to actually belong to the title you're about to "
        "name, not just be ignored because the title matched. A second real example of getting this wrong: OCR "
        "extracted the clean title 'UnBroken' plus credits including 'BETH LANE', 'MAKEMAKE ENTERTAINMENT', and "
        "'HEARTLAND', and a reply confidently identified this as the well-known 2014 war film 'Unbroken' "
        "(director Angelina Jolie, Universal Pictures) - a title-only match that ignored every one of those "
        "credits, none of which have anything to do with that film. The poster was actually for a different, "
        "much less well-known 2023 documentary that happens to share the exact same title. If you have credit "
        "names or a studio/production company in the extracted text, search using THOSE (e.g. 'Beth Lane "
        "documentary UnBroken') rather than the bare title alone, since a generic title can collide with a "
        "famous unrelated work but a specific person or company name usually won't.\n\n"
        "Before you commit to a candidate, write out a short checklist: one line per extracted fragment, each "
        "marked either MATCHES (explains this fragment) or CONTRADICTS/UNEXPLAINED (this fragment doesn't fit "
        "the candidate at all). Only commit to that candidate once every fragment is marked MATCHES or you have "
        "a specific reason a fragment is unrelated (e.g. it's a tagline, not part of the title). If anything is "
        "still CONTRADICTS/UNEXPLAINED after that check, you don't have a real match yet, keep searching or "
        "fall through to Step 3's honest-uncertainty path instead of naming a title anyway.\n\n"
        "Step 3 - report honestly. If Step 1/2 produces a confident full match, present it clearly as an "
        "OCR-derived best guess (not a certainty) and ask the operator to confirm, since the source text was "
        "rough. If no reconstruction produces a match that explains the fragments, say plainly that OCR wasn't "
        "clear enough to identify this one, quote the raw extracted text so the operator can judge for "
        "themselves, and ask them to confirm the title or send a clearer photo rather than guessing. Do the "
        "same when the exact same title matches more than one real entry (sequels, a franchise with numbered "
        "volumes/seasons, or even an unrelated movie and TV series that happen to share a title) and nothing "
        "you extracted distinguishes which one this is: list the candidates with what distinguishes them "
        "(year, medium, subtitle, volume number) instead of picking one for the operator.\n\n"
        "Once you have a specific, justified title candidate (not a guess), check Plex library status. If it's "
        "not in the library yet, offer to add it; don't add it without the operator confirming, same as any "
        "other add."
    )
    return await _handle_message(
        settings,
        token,
        chat_id,
        task,
        state,
        model=PHOTO_IDENTIFY_MODEL,
        max_tool_rounds=PHOTO_IDENTIFY_MAX_TOOL_ROUNDS,
        force_sections={"library", "web"},
    )


async def run_bot() -> None:
    settings = BotSettings()  # type: ignore[call-arg]
    if not settings.nas_ops_telegram_bot_token or not settings.nas_ops_telegram_allowed_chat_id:
        raise SystemExit("NAS_OPS_TELEGRAM_BOT_TOKEN and NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID must be set in .env")
    token = settings.nas_ops_telegram_bot_token.get_secret_value()
    allowed_chat_id = settings.nas_ops_telegram_allowed_chat_id

    await _set_bot_commands(token)
    chat_states = _load_chat_states()
    logger.info(
        "NAS Ops bot polling started (model=%s, %d prior turns loaded)",
        settings.ollama_bot_model,
        len(chat_states.get(allowed_chat_id, ChatState(history=[])).get("history", [])),
    )
    offset: int | None = None
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 10) as http:
        while True:
            try:
                params: dict[str, int] = {"timeout": POLL_TIMEOUT_S}
                if offset is not None:
                    params["offset"] = offset
                resp = await http.get(f"{TELEGRAM_API}/bot{token}/getUpdates", params=params)
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                logger.warning("getUpdates network error, retrying: %s", exc)
                await asyncio.sleep(5)
                continue
            except httpx.HTTPStatusError as exc:
                # 409 = another poller is already using this token (e.g. a
                # second instance briefly overlapping during a restart) —
                # back off and retry instead of crashing; systemd's
                # Restart=always would otherwise just recreate the same race.
                logger.warning("getUpdates HTTP error, retrying: %s", exc)
                await asyncio.sleep(10)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = message.get("text")
                photo = message.get("photo")
                if chat.get("id") != allowed_chat_id:
                    logger.warning("ignored message from unauthorized chat_id=%s", chat.get("id"))
                    continue
                if photo:
                    logger.info("photo message: %d size(s)", len(photo))
                    state = chat_states.get(
                        allowed_chat_id, ChatState(history=[], pending_action=None, known_entity_ids={})
                    )
                    chat_states[allowed_chat_id] = await _handle_photo_message(
                        settings, token, allowed_chat_id, photo, message.get("caption") or "", state
                    )
                    _save_chat_states(chat_states)
                    continue
                if not text:
                    continue
                logger.info("message: %r", text)
                if text.strip().split()[0].split("@")[0] in ("/help", "/start"):
                    # Deterministic, no model call — see HELP_TEXT's own
                    # comment for why this bypasses the LLM entirely.
                    await send_message(token, allowed_chat_id, HELP_TEXT)
                    continue
                state = chat_states.get(
                    allowed_chat_id, ChatState(history=[], pending_action=None, known_entity_ids={})
                )
                chat_states[allowed_chat_id] = await _handle_message(settings, token, allowed_chat_id, text, state)
                _save_chat_states(chat_states)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
