from __future__ import annotations

import asyncio
import logging

import httpx
from fastmcp import Client

from seedbox_mcp.chat.ollama_ai import DEFAULT_OLLAMA_URL, run_agent_turn
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

This is a live conversation with the one person who runs this NAS, not a \
scheduled report — answer their actual question, don't pad it into a \
digest-style report unless they asked for one. Be direct and concise; a \
one-line answer to a one-line question is correct. Use tools whenever the \
answer depends on live state — don't guess.

You have no write or delete tools right now. If asked to change something \
(pause a download, delete media, etc.), say plainly that you can look things \
up but can't act yet.
"""


class BotSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_bot_model: str = DEFAULT_BOT_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def _handle_message(settings: BotSettings, token: str, chat_id: int, text: str) -> None:
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{TELEGRAM_API}/bot{token}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        reply = await run_agent_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            mcp_client=mcp_client,
            model=settings.ollama_bot_model,
            ollama_url=settings.ollama_url,
        )
        logger.info("reply: %r", reply)
    except Exception:
        logger.exception("agent turn failed for message: %r", text)
        reply = "Something went wrong answering that — check the service log (journalctl -u seedbox-telegram-bot)."
    await send_message(token, chat_id, reply)


async def run_bot() -> None:
    settings = BotSettings()  # type: ignore[call-arg]
    if not settings.nas_ops_telegram_bot_token or not settings.nas_ops_telegram_allowed_chat_id:
        raise SystemExit("NAS_OPS_TELEGRAM_BOT_TOKEN and NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID must be set in .env")
    token = settings.nas_ops_telegram_bot_token.get_secret_value()
    allowed_chat_id = settings.nas_ops_telegram_allowed_chat_id

    logger.info("NAS Ops bot polling started (model=%s)", settings.ollama_bot_model)
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
                await _handle_message(settings, token, allowed_chat_id, text)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
