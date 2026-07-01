from __future__ import annotations

import asyncio
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
POLL_TIMEOUT_S = 30

# Conversation history, persisted to disk — this service gets restarted
# often during active development, and losing multi-turn context on every
# redeploy defeats the point of adding it. Keyed by chat_id even though only
# one chat is ever allowed today, so this doesn't need reshaping if that
# changes. Plain JSON, not a database — this is a handful of KB for one
# operator's conversation, not a scaling concern.
#
# Per-chat value is {"history": [...], "pending_action": {...} | null} —
# pending_action travels alongside history because it's the same kind of
# per-conversation state (see ollama_ai.run_agent_turn's pending_action
# param): the single action-tool preview the bot is currently allowed to
# execute on a matching confirm=true. Losing it on restart would just mean
# an in-flight "yes" fails closed and asks for a fresh preview — an
# ok-either-way default rather than a real problem.
HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / ".telegram_bot_history.json"


class ChatState(dict[str, Any]):
    """{"history": list[dict], "pending_action": dict | None} — a thin alias
    so callers don't have to spell out the shape every time."""


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
            # no pending action rather than guessing at one from old data.
            states[key] = ChatState(history=value, pending_action=None)
        elif isinstance(value, dict):
            states[key] = ChatState(history=value.get("history", []), pending_action=value.get("pending_action"))
    return states


def _save_chat_states(states: dict[int, ChatState]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps({str(k): v for k, v in states.items()}))
    except OSError:
        logger.exception("failed to persist history to %s", HISTORY_PATH)


SYSTEM_PROMPT = """\
You are the operator's NAS Ops assistant, talking to them directly over \
Telegram. You have read-only tools covering the whole NAS: Plex, Radarr, \
Sonarr, Tautulli, backup health, and NAS storage outside the Plex library.

For "what are my internet speeds" / "how fast is the NAS's connection" — use \
nas_internet_speed_test. It runs a real test FROM the NAS itself (not from \
wherever this bot runs), takes ~5-10s, and always runs fresh — there's no \
cached result. It consumes real bandwidth (a few hundred MB), so don't call \
it more than once per conversation unless the operator specifically asks \
you to re-test.

For anything about downloads, requests, or "is X available" — prefer the \
nasdoom_* tools (the NASDOOM app's own BFF) over the raw service tools:
- nasdoom_omni_search(query) — the right tool for "do we have X" / "can I \
get X" about one title. Already resolves inLibrary/managed/acquirable.
- nasdoom_queue — unified SABnzbd + arr-import download queue.
- nasdoom_requests_overview — friend-request state with plain-English labels \
(needs_approval, awaiting_release, downloading, available, etc).
- nasdoom_health / nasdoom_control — quick reachability and storage checks.
staleness_report is for library-wide sweeps ("what haven't I watched in 6 \
months"), not single-title lookups — don't reach for it on a one-off question.

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
confirm, and don't treat an unrelated question (like "what did you just \
do?") as a reason to fire one off. If a "yes" doesn't clearly map to one \
specific thing you just previewed, ask what it's confirming instead of \
guessing. If you get the not_permitted rejection, that means there's \
nothing live to confirm — tell the operator, don't retry blindly.

Never invent a specific value for an action parameter (a speedcap percentage, \
a priority level, anything numeric or named) that the operator never actually \
stated anywhere in the conversation — including when you're the one who just \
asked "what level would you like?" and they replied with a bare "yes" or "go \
ahead" instead of an actual number. The harness's confirm-matching can't \
catch this: previewing and confirming a value you made up yourself, in the \
same turn, looks perfectly legitimate to it. If the operator's reply doesn't \
contain the actual value your own question was asking for, ask again instead \
of picking one — don't treat generic agreement as license to fill in a \
specific number yourself.

For non-video content — music samples/kits, software, games, books, none \
of which have an arr or a TMDB catalog — use nasdoom_find(query, scope) to \
search, then nasdoom_find_grab(grab_id, ...) to download (same confirm=false \
preview-first pattern; the preview can't re-verify the title since grab_ids \
are single-use, so double-check it's the right result from the find call \
before confirming). grab_ids expire in 30 minutes — if a grab comes back \
expired, just search again. share=true on the grab routes it into the \
shared/Transfer folder instead of your private library — only set that if \
the operator actually wants it shared, default is private.

For the friend file-share portal (files.logueos.xyz): nasdoom_share_friends_list \
and nasdoom_share_files_list are read-only lookups. nasdoom_share_friend_create \
makes a real account with a real password (upload=false is download-only, \
upload=true also lets them drop new files into the shared folder — they \
can never overwrite/delete/browse elsewhere/rename/share regardless). \
nasdoom_share_friend_revoke removes access. Both take confirm=false|true — \
same preview-first pattern as everything else. Creating an account hands \
out real credentials to a real person, so read back the name to the \
operator before confirming if there's any ambiguity about who it's for. \
There's no tool to delete a shared file — that's a deliberate gap, ask the \
operator to do it directly if it comes up.

For anything broken that's outside these tools — a failed backup, a service \
that's down, config drift — call escalate_to_worker with a clear \
description, tell the operator you escalated it and why, then move on. \
Don't try to fix system-level things yourself; you don't have the tools \
for that, and pretending otherwise wastes their time. This includes when \
the operator tells you something is broken but your own read-only check \
says it's fine — don't just trust the tool and dismiss what they said; a \
tool reporting "ok" doesn't mean the operator is wrong, it might mean the \
tool isn't seeing what they're seeing. Say the discrepancy out loud and \
escalate it for a closer look rather than resolving the conflict yourself. \
And never hand the operator raw commands to run themselves (journalctl, \
systemctl, anything shell-level) as your answer — that's the exact "you \
don't have the tools for that" situation above; escalate instead of \
outsourcing the investigation back to them.

When you're recommending what to do about something outside your tools' \
reach — especially deleting or removing anything — never suggest a manual, \
ungated path (removing files/folders directly, running a script, editing \
config by hand) even as a "here's how you'd do it yourself" aside. If \
there's no tool-gated way to do it, say that plainly and either escalate it \
or leave it for the operator to decide how, but don't hand them an \
unaudited shortcut around the same safety rails your own tools have.

This is a live conversation with the one person who runs this NAS, not a \
scheduled report — answer their actual question, don't pad it into a \
digest-style report unless they asked for one. Be direct and concise; a \
one-line answer to a one-line question is correct. Use tools whenever the \
answer depends on live state — don't guess.
"""


class BotSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_bot_model: str = DEFAULT_BOT_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def _handle_message(
    settings: BotSettings, token: str, chat_id: int, text: str, state: ChatState
) -> ChatState:
    """Runs one turn and returns the updated chat state — on error, returns
    `state` unchanged (a failed turn shouldn't inject partial/broken state
    into the next one)."""
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{TELEGRAM_API}/bot{token}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        reply, new_history, new_pending_action = await run_agent_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            mcp_client=mcp_client,
            model=settings.ollama_bot_model,
            allowed_tools=READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS,
            history=state.get("history", []),
            pending_action=state.get("pending_action"),
            ollama_url=settings.ollama_url,
        )
        logger.info("reply: %r", reply)
    except Exception:
        logger.exception("agent turn failed for message: %r", text)
        await send_message(
            token,
            chat_id,
            "Something went wrong answering that — check the service log (journalctl -u seedbox-telegram-bot).",
        )
        return state
    await send_message(token, chat_id, reply)
    return ChatState(history=trim_history(new_history), pending_action=new_pending_action)


async def run_bot() -> None:
    settings = BotSettings()  # type: ignore[call-arg]
    if not settings.nas_ops_telegram_bot_token or not settings.nas_ops_telegram_allowed_chat_id:
        raise SystemExit("NAS_OPS_TELEGRAM_BOT_TOKEN and NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID must be set in .env")
    token = settings.nas_ops_telegram_bot_token.get_secret_value()
    allowed_chat_id = settings.nas_ops_telegram_allowed_chat_id

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
                if chat.get("id") != allowed_chat_id:
                    logger.warning("ignored message from unauthorized chat_id=%s", chat.get("id"))
                    continue
                if not text:
                    continue
                logger.info("message: %r", text)
                state = chat_states.get(allowed_chat_id, ChatState(history=[], pending_action=None))
                chat_states[allowed_chat_id] = await _handle_message(settings, token, allowed_chat_id, text, state)
                _save_chat_states(chat_states)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
