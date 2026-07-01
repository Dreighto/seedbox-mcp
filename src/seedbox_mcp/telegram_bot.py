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
HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / ".telegram_bot_history.json"


def _load_history() -> dict[int, list[dict[str, Any]]]:
    if not HISTORY_PATH.exists():
        return {}
    try:
        raw = json.loads(HISTORY_PATH.read_text())
        return {int(chat_id): turns for chat_id, turns in raw.items()}
    except (json.JSONDecodeError, ValueError, OSError):
        logger.exception("failed to load %s, starting with empty history", HISTORY_PATH)
        return {}


def _save_history(history_by_chat: dict[int, list[dict[str, Any]]]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps({str(k): v for k, v in history_by_chat.items()}))
    except OSError:
        logger.exception("failed to persist history to %s", HISTORY_PATH)


SYSTEM_PROMPT = """\
You are the operator's NAS Ops assistant, talking to them directly over \
Telegram. You have read-only tools covering the whole NAS: Plex, Radarr, \
Sonarr, Tautulli, backup health, and NAS storage outside the Plex library.

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
again with confirm=true. Use these when the operator asks directly ("pause \
the queue") or when it's the obvious next step in the conversation — you \
don't need to ask the operator's permission for something this reversible \
(the confirm=false preview step already covers that), but say plainly what \
you did. If the preview shows something unexpected (wrong item matched, \
found=false), stop and ask the operator instead of forcing confirm=true.

For non-video content — music samples/kits, software, games, books, none \
of which have an arr or a TMDB catalog — use nasdoom_find(query, scope) to \
search, then nasdoom_find_grab(grab_id, ...) to download (same confirm=false \
preview-first pattern; the preview can't re-verify the title since grab_ids \
are single-use, so double-check it's the right result from the find call \
before confirming). grab_ids expire in 30 minutes — if a grab comes back \
expired, just search again. share=true on the grab routes it into the \
shared/Transfer folder instead of your private library — only set that if \
the operator actually wants it shared, default is private.

For anything broken that's outside these tools — a failed backup, a service \
that's down, config drift — call escalate_to_worker with a clear \
description, tell the operator you escalated it and why, then move on. \
Don't try to fix system-level things yourself; you don't have the tools \
for that, and pretending otherwise wastes their time.

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
    settings: BotSettings, token: str, chat_id: int, text: str, history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Runs one turn and returns the updated history — on error, returns
    `history` unchanged (a failed turn shouldn't inject partial/broken state
    into the next one)."""
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{TELEGRAM_API}/bot{token}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        reply, new_history = await run_agent_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            mcp_client=mcp_client,
            model=settings.ollama_bot_model,
            allowed_tools=READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS,
            history=history,
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
        return history
    await send_message(token, chat_id, reply)
    return trim_history(new_history)


async def run_bot() -> None:
    settings = BotSettings()  # type: ignore[call-arg]
    if not settings.nas_ops_telegram_bot_token or not settings.nas_ops_telegram_allowed_chat_id:
        raise SystemExit("NAS_OPS_TELEGRAM_BOT_TOKEN and NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID must be set in .env")
    token = settings.nas_ops_telegram_bot_token.get_secret_value()
    allowed_chat_id = settings.nas_ops_telegram_allowed_chat_id

    history_by_chat = _load_history()
    logger.info(
        "NAS Ops bot polling started (model=%s, %d prior turns loaded)",
        settings.ollama_bot_model,
        len(history_by_chat.get(allowed_chat_id, [])),
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
                chat_history = history_by_chat.get(allowed_chat_id, [])
                history_by_chat[allowed_chat_id] = await _handle_message(
                    settings, token, allowed_chat_id, text, chat_history
                )
                _save_history(history_by_chat)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
