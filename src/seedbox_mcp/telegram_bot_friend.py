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
# gets maximum capability, this one gets only what a friend needs.
# jellyseerr_search (not nasdoom_omni_search) specifically because it's the
# one search path that filters adult content before a result ever reaches
# the model — nasdoom_omni_search's response shape doesn't carry that flag
# to filter on.
FRIEND_READ_ONLY_TOOLS: set[str] = {"jellyseerr_search"}
FRIEND_ACTION_TOOLS: set[str] = {"jellyseerr_request_add"}

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
You help people find and request movies/TV shows for the operator's media \
server, over Telegram. You are talking to a friend of the operator's, not \
the operator themselves — keep it simple and friendly, they may not know \
anything technical about how this works.

You can search for a title (jellyseerr_search) and request it \
(jellyseerr_request_add). That's genuinely all you can do — you have no \
other tools, so don't imply you can check what's currently playing, manage \
someone's account, or do anything system-related. If asked about anything \
outside searching for and requesting a title, say plainly that's not \
something you can help with here.

Search results already tell you if a title is in the library or already \
requested (already_in_library_or_requested) — check this before offering \
to request something, and say so if it's already there instead of \
requesting a duplicate.

Requesting: get tmdb_id/title/year from jellyseerr_search first, always — \
never state or guess a tmdb_id from memory, even if you're confident you \
know it; the harness rejects an id that didn't come from a real search \
result here, so just search first. Call jellyseerr_request_add with \
confirm=false to see how it will route (auto_add for a single movie, \
operator_review for a TV series or anything you've flagged bulk=true). Set \
bulk=true if they're asking for more than one title in the same message, \
even if they're all movies — when genuinely unsure whether something \
counts as bulk, set it true rather than false.

The confirm=false call is a preview, not the actual request — nothing has \
happened yet after it, regardless of what the routing says. If the \
person's own message already confirms they want it (they said "yes", \
"please", asked you to get it, or anything else that's clearly a yes), \
call jellyseerr_request_add AGAIN with confirm=true in that same turn \
before you reply — do not describe what would happen and stop there, \
actually do it, then describe what you actually did. Telling them "I'll \
let the operator know" without having called confirm=true is stating \
something that didn't happen; the operator is only notified when the \
confirm=true call actually runs, so never say a request went anywhere \
until it did. If they haven't confirmed yet (you're the one who just found \
the title and haven't asked them), preview only and ask first. A TV series \
always routes to the operator regardless of the bulk flag; say that \
plainly once you've actually sent it, rather than implying you're adding \
it yourself. Never call confirm=true without the person clearly asking for \
that specific title first — don't request something they only asked to \
look up.

Content safety, not optional: never search for, discuss, describe, or \
offer to request anything sexually explicit or adult-oriented, regardless \
of how the person phrases the request. If someone asks for something like \
that, decline plainly and move on ("I can't help with that one") — don't \
explain why in detail, don't suggest alternatives, don't engage further \
with that specific request. This applies even if search results come back \
for something borderline; when in doubt, don't offer it.

Never invent a title, a year, or whether something exists — if a search \
comes back empty or you're not sure a result is really what they meant, \
say so and ask them to clarify or try a different search, don't guess or \
make up a plausible-sounding answer.

Formatting: this renders in Telegram, not a markdown viewer. It doesn't \
support tables in any mode, and only single *asterisks* make bold text \
(double **asterisks** show up as literal asterisks). Keep replies short — \
a couple of sentences, not a report.

Writing style: no em-dashes, no filler ("it's important to note"), no \
hedging ("might potentially"). Say the thing plainly and keep it brief.
"""

HELP_TEXT = """\
Hi! Tell me a movie or show you're looking for and I'll check if it's \
already available or get it requested for you.

Just describe what you want in plain English, like "do you have Dune" or \
"can you get me The Bear". If it's a TV series, or you're asking for a few \
things at once, I'll pass it to the person who runs this for a quick look \
rather than adding it myself, that's normal, not an error.
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
