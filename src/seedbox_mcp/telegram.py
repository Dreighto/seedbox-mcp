from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("seedbox_mcp.telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram's hard cap is 4096; leave headroom for truncation marker


async def send_message(token: str, chat_id: int, text: str) -> None:
    body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 20] + "\n\n[truncated]"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": body},
        )
    if resp.is_error:
        logger.error("telegram sendMessage failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
