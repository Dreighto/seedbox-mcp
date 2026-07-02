from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client

from seedbox_mcp.chat.ollama_ai import DEFAULT_OLLAMA_URL, run_agent_turn, trim_history
from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import TELEGRAM_API, send_message

logger = logging.getLogger("seedbox_mcp.telegram_bot_friend")

# Started as the fast/cheap model on the assumption that search + routing
# is a quick lookup, not multi-step reasoning. Live testing proved that
# wrong before this ever shipped: a plain single-title search worked but
# was slow and produced a garbled reply fragment, and a TV-series request
# (the exact case that has to route correctly to the operator, not
# auto-add) hung completely with zero tool calls for over 90 seconds.
# Same failure class already found and fixed twice tonight (poster ID,
# the monitor's queue-check) — this is a safety-relevant routing decision
# (does this get auto-added or does it need the operator's eyes on it),
# so it gets the reliable model from the start rather than shipping on
# the fast one and hoping.
DEFAULT_FRIEND_BOT_MODEL = "qwen3-coder:480b-cloud"
POLL_TIMEOUT_S = 30

# Deliberately its own small, curated tool set instead of sharing
# telegram_bot.py's — this is the "restrict down" side of the two-tier
# design (see telegram_bot.py's ACTION_TOOLS comment): the operator bot
# gets maximum capability, this one gets only what an outside requester
# needs. jellyseerr_search (not nasdoom_omni_search) specifically because
# it's the one search path that filters adult content before a result ever
# reaches the model. nasdoom_releases is read-only (see quality, warn about
# theatrical rips). The only writes are the standard request (respects the
# 720p/1080p quality profile) and a specific-release grab used ONLY after a
# requester knowingly accepts a below-standard quality — both acquire media
# but neither can touch the OS, services, config, storage, other users'
# data, or the file-share portal.
FRIEND_READ_ONLY_TOOLS: set[str] = {
    "jellyseerr_search",
    "web_search",
    "content_release_status",
    "nasdoom_releases",
}
FRIEND_ACTION_TOOLS: set[str] = {"jellyseerr_request_add", "nasdoom_grab_release"}

# HARD GUARD. This bot is exposed to people outside the network, so its tool
# set must NEVER drift to include anything system-affecting. The full set is
# asserted (at import) to be a subset of this explicit safe allowlist — so
# adding a tool to the bot requires deliberately adding it here too — AND to
# be disjoint from a denylist of everything dangerous. If either check
# fails the module refuses to load rather than silently exposing a risky
# tool to strangers.
_FRIEND_SAFE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "jellyseerr_search",
        "web_search",
        "content_release_status",
        "nasdoom_releases",
        "jellyseerr_request_add",
        "nasdoom_grab_release",
    }
)
# A representative denylist of the categories that must never reach this bot
# (host control, queue/blocklist writes, library add with arbitrary profile,
# the friend file-share portal's account tools, escalation, Plex match). Not
# exhaustive of every tool name, but any of these appearing here is a
# tripwire; the subset check against the allowlist is the real containment.
_FRIEND_FORBIDDEN: frozenset[str] = frozenset(
    {
        "nas_service_restart",
        "nas_service_status",
        "nas_disk_health",
        "nas_import_diagnosis",
        "escalate_to_worker",
        "nasdoom_queue_command",
        "nasdoom_queue_item_command",
        "nasdoom_add",
        "nasdoom_find_grab",
        "nasdoom_match_apply",
        "nasdoom_share_friend_create",
        "nasdoom_share_friend_revoke",
        "radarr_blocklist_remove",
        "sonarr_blocklist_remove",
    }
)
_friend_tools = FRIEND_READ_ONLY_TOOLS | FRIEND_ACTION_TOOLS
if not _friend_tools <= _FRIEND_SAFE_ALLOWLIST:
    raise RuntimeError(
        f"Friend bot tool set escaped the safe allowlist: {_friend_tools - _FRIEND_SAFE_ALLOWLIST}. "
        "This bot is exposed to outside users; every tool must be on _FRIEND_SAFE_ALLOWLIST."
    )
if _friend_tools & _FRIEND_FORBIDDEN:
    raise RuntimeError(
        f"Friend bot tool set includes forbidden system tools: {_friend_tools & _FRIEND_FORBIDDEN}."
    )

# Separate history file from the operator bot's — different conversations,
# different chat_ids, no reason to share state.
HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / ".telegram_bot_friend_history.json"


class ChatState(dict[str, Any]):
    """Same shape as telegram_bot.py's ChatState — {"history": [...],
    "pending_action": {...} | None, "known_entity_ids": {...}}."""


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
        if isinstance(value, dict):
            states[key] = ChatState(
                history=value.get("history", []),
                pending_action=value.get("pending_action"),
                known_entity_ids=value.get("known_entity_ids", {}),
            )
    return states


def _save_chat_states(states: dict[int, ChatState]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps({str(k): v for k, v in states.items()}))
    except OSError:
        logger.exception("failed to persist history to %s", HISTORY_PATH)


SYSTEM_PROMPT = """\
You help people find, ask about, and request movies, TV shows, and anime \
for the operator's Plex server, over Telegram. You are talking to a friend \
of the operator's, not the operator — most of them are not technical and \
have no idea how any of this works. Keep every reply short, warm, and in \
plain words. Never mention tool names, IDs, quality profiles, indexers, or \
anything under the hood.

What you can do, and nothing beyond it:
1. Check if something is already on Plex, or find a title (jellyseerr_search).
2. Request something to be added (jellyseerr_request_add).
3. Check what quality is actually available for a title right now \
(nasdoom_releases) and, only with the person's explicit okay, grab a \
specific copy (nasdoom_grab_release).
4. Look up whether something is out yet or streaming yet \
(content_release_status — a web-grounded lookup that gives release dates \
and streaming status directly).
If someone asks for anything else — account help, playback problems, \
server settings, "what's playing right now" — say plainly that's not \
something you can do here, and that they should message the owner directly.

Always re-check availability with jellyseerr_search on the CURRENT turn \
for any question about whether something exists, is out, is on Plex, or \
can be watched — every single time, with NO exceptions. This includes the \
message right after you just answered: if you tell someone a title's \
status and their next message is "can I watch it?" or "is it ready?", you \
must call jellyseerr_search AGAIN before answering, even though you just \
checked seconds ago — do NOT reuse the result you just got. This bot runs \
as a separate private chat per person and cannot see other people's \
threads; the shared library changes because of what OTHERS request and \
download, so the only way your answer stays true is to re-read the live \
state on every availability question. Never answer such a question from \
earlier in this conversation, from something you said before, or from your \
own knowledge. Use the conversation only to understand WHICH title they \
mean (so "can I watch it?" refers back to the title just discussed), then \
run a fresh search and answer from that result. If you did not call \
jellyseerr_search this turn, you are not allowed to state whether \
something is on Plex.

Answering "do you have X" / "is X on Plex": search first \
(jellyseerr_search) and read the availability field precisely — this is \
where honesty matters most. ONLY say a title is on Plex / ready to watch \
when streamable_now is true (availability "available", or \
"partially_available" for a series with some episodes). Every other state \
means it is NOT watchable yet, and you must say so plainly, not call it \
"on Plex":
- "downloading": a copy was found and is actively downloading right now, \
not ready yet ("it's downloading now, should be ready soon").
- "approved_waiting_for_release": it's on the list and set to grab \
automatically once a copy exists, but nothing is downloading yet — usually \
because it hasn't been released, or no copy is out there yet. Say it that \
way: "it's lined up and will download automatically once it's out / a copy \
is available", NOT "downloading now" (nothing is downloading, and saying so \
makes people think it's stuck). If it's simply not released yet, say that \
plainly.
- "requested_pending_approval": someone asked for it, still waiting on the \
owner, not ready.
- "not_available" / "not_in_library": not on the server; offer to request it.
A title being in the system (requested, downloading, or just on a \
watchlist) is NOT the same as being watchable — never tell someone they \
can watch something unless streamable_now is true. Never guess a title, a \
year, or whether something exists; if the search is empty or unclear, ask \
them to clarify rather than making something up.

"Is the new season / new batch of <anime or show> out yet?" or "is <new \
movie> out yet?": use content_release_status to check whether it has \
actually released or started streaming, and answer honestly, including \
"not out yet" when that's the truth. Don't promise something that hasn't \
been released. (web_search is still available for the rare general \
question, but for release/streaming timing use content_release_status.)

Requesting (the normal path): get the title's id from jellyseerr_search \
first, always — never state or guess an id from memory; the system rejects \
one that didn't come from a real search here, so just search. Before \
requesting, check availability: if it's already "available" it's on Plex, \
just tell them to watch it; if it's "downloading", \
"approved_waiting_for_release", or "requested_pending_approval" it's \
already handled, tell them that (in the honest wording above) instead of \
making a duplicate request. Only actually request when it's \
"not_in_library" or "not_available". Call \
jellyseerr_request_add with confirm=false to see how it routes (a single \
movie adds automatically at the server's normal quality; a TV series, or \
several titles at once, goes to the owner to approve). Set bulk=true if \
they asked for more than one title in one message. The confirm=false call \
is only a preview — nothing has happened yet. If their message already \
says yes (they asked you to get it, said "please", "yes", etc.), call \
jellyseerr_request_add AGAIN with confirm=true in the same turn, then tell \
them what actually happened. Never say a request was sent or the owner was \
notified unless the confirm=true call actually ran. If they only asked to \
look something up, don't request it — ask first.

Quality honesty (this matters — it prevents complaints later): the normal \
request only grabs a proper copy at the server's standard quality \
(roughly 1080p). For something very new — a movie that just hit theaters, \
say — a proper copy often does not exist yet, and the only thing available \
is a camcorder/telesync/screener rip that looks noticeably worse than real \
streaming quality. When a request is for something that new, check \
nasdoom_releases: if standard_quality_available is true, just request it \
normally. If only_theatrical_rips is true (or the only options are flagged \
theatrical_rip), tell the person plainly and up front, in normal words, \
that the only copy out right now is a low-quality theater recording, not \
true streaming quality, and ask if they still want it. ONLY if they \
clearly say yes to that, grab that specific copy with nasdoom_grab_release \
(confirm=false to preview, then confirm=true). Never grab a low-quality \
copy without that explicit "yes, I know it's low quality" — and always \
prefer the normal request when a proper copy is or will be available.

Content safety, not optional: never search for, describe, or offer \
anything sexually explicit or adult. If asked, just say "I can't help with \
that one" and move on — no detail, no alternatives.

Formatting: plain Telegram text. No tables, and only single *asterisks* \
for bold (double **asterisks** show up literally). A couple of short \
sentences, never a report.

Writing style: no em-dashes, no filler ("it's important to note"), no \
hedging ("might potentially"), no corporate words. Say the thing plainly.
"""

HELP_TEXT = """\
Hey! I help you find stuff for the Plex server. Here's what I can do, in \
plain terms:

- Tell you if a movie, show, or anime is already on Plex.
- Request something new to be added. Just ask, like "can you get Dune" or \
"do you have The Bear".
- Check if a new movie, or a new season or batch of an anime, is actually \
out yet or streaming yet.
- Grab something that just came out. Heads up: if a movie only just hit \
theaters, sometimes the only copy available is a rough camera recording, \
not real streaming quality. I'll always tell you first and let you decide.

If it's a TV series or a few things at once, I pass it to the owner to \
approve, that's normal. Anything else (account help, playback issues) you'll \
want to message the owner directly. Just tell me what you're looking for.
"""


async def _set_bot_commands(token: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{TELEGRAM_API}/bot{token}/setMyCommands",
            json={"commands": [{"command": "help", "description": "What can you do?"}]},
        )
    if resp.is_error:
        logger.warning("setMyCommands failed (non-fatal): %s %s", resp.status_code, resp.text)


class FriendBotSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_friend_bot_model: str = DEFAULT_FRIEND_BOT_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def _handle_message(
    settings: FriendBotSettings, token: str, chat_id: int, text: str, state: ChatState
) -> ChatState:
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{TELEGRAM_API}/bot{token}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        reply, new_history, new_pending_action, new_known_entity_ids = await run_agent_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            mcp_client=mcp_client,
            model=settings.ollama_friend_bot_model,
            allowed_tools=FRIEND_READ_ONLY_TOOLS | FRIEND_ACTION_TOOLS,
            action_tools=FRIEND_ACTION_TOOLS,
            history=state.get("history", []),
            pending_action=state.get("pending_action"),
            known_entity_ids=state.get("known_entity_ids"),
            ollama_url=settings.ollama_url,
            max_tool_rounds=10,
        )
        logger.info("reply: %r", reply)
    except Exception:
        logger.exception("agent turn failed for message: %r", text)
        await send_message(token, chat_id, "Something went wrong there, try asking again?")
        return state
    if not reply.strip():
        logger.warning("agent turn produced an empty reply for message: %r", text)
        await send_message(token, chat_id, "Sorry, I didn't catch that, could you try rephrasing?")
        return ChatState(
            history=trim_history(new_history), pending_action=new_pending_action, known_entity_ids=new_known_entity_ids
        )
    await send_message(token, chat_id, reply)
    return ChatState(
        history=trim_history(new_history), pending_action=new_pending_action, known_entity_ids=new_known_entity_ids
    )


async def run_bot() -> None:
    settings = FriendBotSettings()  # type: ignore[call-arg]
    if not settings.nasdoom_helper_telegram_bot_token:
        raise SystemExit("NASDOOM_HELPER_TELEGRAM_BOT_TOKEN must be set in .env")
    token = settings.nasdoom_helper_telegram_bot_token.get_secret_value()
    allowed_chat_ids = set(settings.nasdoom_helper_telegram_allowed_chat_ids)
    if not allowed_chat_ids:
        logger.warning(
            "NASDOOM_HELPER_TELEGRAM_ALLOWED_CHAT_IDS is empty — every message will be ignored until "
            "a friend's chat_id is added"
        )

    await _set_bot_commands(token)
    chat_states = _load_chat_states()
    logger.info(
        "Friend bot polling started (model=%s, %d allowed chat_ids)",
        settings.ollama_friend_bot_model,
        len(allowed_chat_ids),
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
                logger.warning("getUpdates HTTP error, retrying: %s", exc)
                await asyncio.sleep(10)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = message.get("text")
                if chat_id not in allowed_chat_ids:
                    # Not an error state — this is the bootstrap path for a
                    # new friend: they message the bot, this logs their
                    # chat_id, the operator adds it to
                    # NASDOOM_HELPER_TELEGRAM_ALLOWED_CHAT_IDS and restarts.
                    logger.info("message from unrecognized chat_id=%s (not yet allowlisted)", chat_id)
                    continue
                if not text:
                    continue
                logger.info("message from chat_id=%s: %r", chat_id, text)
                if text.strip().split()[0].split("@")[0] in ("/help", "/start"):
                    await send_message(token, chat_id, HELP_TEXT)
                    continue
                state = chat_states.get(chat_id, ChatState(history=[], pending_action=None, known_entity_ids={}))
                chat_states[chat_id] = await _handle_message(settings, token, chat_id, text, state)
                _save_chat_states(chat_states)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
