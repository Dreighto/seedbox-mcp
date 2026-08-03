from __future__ import annotations

import asyncio
import sys

from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import send_message


async def send_to_operator(text: str) -> bool:
    """Best-effort push to the operator's own Telegram (the NAS Ops bot's
    token/chat, not any other bot's). Returns True on success, False if the
    NAS Ops bot isn't configured. Importable directly by any seedbox process
    that needs to reach the operator without a chat loop of its own — e.g.
    the friend bot alerting about a dead model over the operator's own bot
    rather than its own (the operator may never have messaged the friend
    bot, so it can't push to them)."""
    settings = Settings()  # type: ignore[call-arg]
    tok = settings.nas_ops_telegram_bot_token
    chat = settings.nas_ops_telegram_allowed_chat_id
    if not tok or not chat:
        return False
    await send_message(tok.get_secret_value(), chat, text)
    return True


async def _send(text: str) -> int:
    ok = await send_to_operator(text)
    if not ok:
        print("nas_ops telegram not configured", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    """Send a plain message to the operator's Telegram. Reusable by any
    seedbox process (and by a dispatched fix agent) that needs to reach the
    operator. Usage: seedbox-notify-operator "message text"."""
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print('usage: seedbox-notify-operator "message"', file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_send(text)))


if __name__ == "__main__":
    main()
