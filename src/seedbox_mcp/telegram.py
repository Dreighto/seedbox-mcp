from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger("seedbox_mcp.telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram's hard cap is 4096; leave headroom for truncation marker

_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _markdown_table_to_lines(table_lines: list[str]) -> list[str]:
    """A markdown table Telegram can't render at all (no parse_mode supports
    it) reduced to plain "col1: col2 (col3)" lines — loses grid alignment
    but stays readable, instead of showing up as literal pipe characters."""
    rows = [
        [cell.strip() for cell in m.group(1).split("|")]
        for line in table_lines
        if (m := _TABLE_ROW_RE.match(line)) and not _TABLE_SEPARATOR_RE.match(line)
    ]
    if len(rows) < 2:
        return table_lines  # didn't parse as expected — leave it alone rather than mangle it
    header, *data_rows = rows
    out = []
    for row in data_rows:
        parts = [f"{header[i]}: {cell}" for i, cell in enumerate(row) if i < len(header) and cell]
        out.append(" · ".join(parts))
    return out


def format_for_telegram(text: str) -> str:
    """Normalizes the model's GitHub-flavored markdown into something
    Telegram's legacy "Markdown" parse mode actually renders, instead of
    showing up as literal asterisks/pipes in the chat. Telegram support is
    much narrower than GFM: **bold** must be single *bold*, and there is no
    table syntax in any parse mode — models trained on GFM output both
    constantly."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # No em/en-dashes in any bot reply (operator's standing no-slop rule). The
    # model is told this in its prompt but ignores it often enough that we
    # enforce it deterministically here — applies to BOTH bots via send_message.
    # A numeric range keeps a hyphen; any other dash becomes a comma, which
    # reads naturally in place of the em-dash the model reaches for.
    text = re.sub(r"(?<=\d)\s*–\s*(?=\d)", "-", text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)

    lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        if _TABLE_ROW_RE.match(lines[i]):
            block = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            out_lines.extend(_markdown_table_to_lines(block))
        else:
            out_lines.append(lines[i])
            i += 1
    return "\n".join(out_lines)


async def send_message(token: str, chat_id: int, text: str) -> None:
    formatted = format_for_telegram(text)
    body = formatted if len(formatted) <= MAX_MESSAGE_CHARS else formatted[: MAX_MESSAGE_CHARS - 20] + "\n\n[truncated]"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": body, "parse_mode": "Markdown"},
        )
        if resp.status_code == 400:
            # The model's output doesn't always produce balanced */_ pairs
            # (an unescaped stray asterisk anywhere breaks Telegram's
            # Markdown parser entirely, rejecting the whole message) — fall
            # back to plain text rather than losing the reply outright.
            logger.warning("telegram sendMessage parse_mode rejected (%s), retrying as plain text", resp.text[:200])
            resp = await client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": body},
            )
    if resp.is_error:
        logger.error("telegram sendMessage failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
