from __future__ import annotations

import logging
from typing import Any

import httpx

from seedbox_mcp.telegram import TELEGRAM_API

logger = logging.getLogger("seedbox_mcp.bot_common")


class ChatState(dict[str, Any]):
    """{"history": list[dict], "pending_action": dict | None,
    "known_entity_ids": dict[str, list[int]]} — a thin alias so callers
    don't have to spell out the shape every time."""


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


async def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Dismisses an inline-keyboard button's loading spinner, optionally
    showing a brief toast. Best-effort — a failed toast shouldn't stop the
    caller from doing the actual work the tap requested."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{TELEGRAM_API}/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
        )
    if resp.is_error:
        logger.warning("answerCallbackQuery failed (non-fatal): %s %s", resp.status_code, resp.text)


async def _download_telegram_photo(token: str, file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=20.0) as http:
        resp = await http.get(f"{TELEGRAM_API}/bot{token}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        resp = await http.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        resp.raise_for_status()
        return resp.content
