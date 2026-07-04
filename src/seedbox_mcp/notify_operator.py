from __future__ import annotations

import asyncio
import sys

from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import send_message


async def _send(text: str) -> int:
    settings = Settings()  # type: ignore[call-arg]
    tok = settings.nas_ops_telegram_bot_token
    chat = settings.nas_ops_telegram_allowed_chat_id
    if not tok or not chat:
        print("nas_ops telegram not configured", file=sys.stderr)
        return 1
    await send_message(tok.get_secret_value(), chat, text)
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
