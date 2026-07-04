from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
import time
from pathlib import Path

from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import send_message

logger = logging.getLogger("seedbox_mcp.friend_error_watch")

REPO = "/home/dreighto/dev/seedbox-mcp"
STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".friend_error_watch_state.json"
WATCHED_UNITS = ["seedbox-telegram-friend-bot.service", "seedbox-friend-notify.service"]

# Same bug re-firing shouldn't re-dispatch a fix agent for this long.
SIGNATURE_COOLDOWN_S = 6 * 3600
# Never launch two fix agents within this window (avoid pile-up if several
# distinct errors land at once — the first agent likely covers the area).
DISPATCH_GAP_S = 20 * 60

# What counts as a real error worth a fix agent: an actual traceback, or the
# bot's own catch-all exception log. Plain warnings/info are ignored.
ERROR_MARKERS = ("Traceback (most recent call last)", "agent turn failed", "CRITICAL")


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError:
        logger.exception("failed to persist error-watch state")


def _journal_since(cursor: str | None) -> list[dict]:
    args = ["journalctl", "-o", "json", "--no-pager"]
    for u in WATCHED_UNITS:
        args += ["-u", u]
    args += ["--after-cursor", cursor] if cursor else ["--since", "5 min ago"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        logger.exception("journalctl read failed")
        return []
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _message(entry: dict) -> str:
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):  # journald can return a byte-array message
        try:
            msg = bytes(msg).decode(errors="replace")
        except (ValueError, TypeError):
            msg = str(msg)
    return str(msg)


def _signature(unit: str, msg: str) -> str:
    """Stable id for an error: the unit + the exception's last line + deepest
    frame, so the same bug maps to one signature across repeats (and cooldown)."""
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    last = lines[-1] if lines else msg[:120]
    frame = next((ln for ln in reversed(lines) if ln.strip().startswith("File ")), "")
    return hashlib.sha256(f"{unit}|{last}|{frame}".encode()).hexdigest()[:16]


def _fix_prompt(unit: str, msg: str) -> str:
    return (
        "A bug just hit the LIVE NASDOOM friend Telegram bot during beta testing (a real "
        f"user is using it). Fix it.\n\nFailing service: {unit}\n\nError / traceback from the "
        f"journal:\n{msg[:3000]}\n\n"
        f"You are working in {REPO} — the repo for the friend bot (src/seedbox_mcp/"
        "telegram_bot_friend.py), the request tool (tools/nasdoom.py: nasdoom_friend_request), "
        "and the notifier (friend_notify.py). Do this:\n"
        "1. Diagnose the root cause from the traceback.\n"
        "2. Make a MINIMAL, targeted fix on a NEW branch (fix/friend-bot-<short-slug>).\n"
        "3. Run the tests: .venv/bin/python -m pytest -q — they must pass.\n"
        "4. Commit to the branch. DO NOT push, DO NOT restart the live services, DO NOT deploy, "
        "and DO NOT touch anything outside this repo. The operator approves the deploy.\n"
        "5. When done, notify the operator by running EXACTLY:\n"
        "   .venv/bin/seedbox-notify-operator \"<plain-English: the bug, your fix, the branch "
        "name, and whether tests passed>\"\n"
        "If you cannot confidently fix it, still run seedbox-notify-operator with what you found "
        "and what you'd need. Keep the operator's reply plain and non-technical."
    )


def _dispatch_fix_agent(unit: str, msg: str) -> None:
    prompt = _fix_prompt(unit, msg)
    # Detached headless agent. unset ANTHROPIC_API_KEY → use the OAuth session
    # (the box's headless-claude pattern). Scoped to the repo via the prompt;
    # works on a branch and never deploys, so it can't push a bad fix live.
    cmd = (
        f"cd {shlex.quote(REPO)} && unset ANTHROPIC_API_KEY && "
        f"claude -p {shlex.quote(prompt)} --permission-mode acceptEdits "
        "--allowedTools Bash Edit Write Read Grep Glob"
    )
    log = open("/tmp/friend_fix_agent.log", "a")  # noqa: SIM115 — lives for the detached child
    subprocess.Popen(
        ["nohup", "bash", "-lc", cmd],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("dispatched fix agent for %s (%s)", unit, _signature(unit, msg))


async def run_once() -> None:
    state = _load_state()
    entries = _journal_since(state.get("cursor"))
    if not entries:
        return
    state["cursor"] = entries[-1].get("__CURSOR", state.get("cursor"))
    seen: dict = state.get("seen", {})
    now = time.time()
    last_dispatch = state.get("last_dispatch_ts", 0)

    settings = Settings()  # type: ignore[call-arg]
    tok = settings.nas_ops_telegram_bot_token
    op_token = tok.get_secret_value() if tok else None
    op_chat = settings.nas_ops_telegram_allowed_chat_id

    for entry in entries:
        msg = _message(entry)
        if not any(marker in msg for marker in ERROR_MARKERS):
            continue
        unit = entry.get("_SYSTEMD_UNIT", "friend-bot")
        sig = _signature(unit, msg)
        if now - seen.get(sig, 0) < SIGNATURE_COOLDOWN_S:
            continue  # same bug already handled recently
        seen[sig] = now
        summary = next((ln for ln in reversed(msg.splitlines()) if ln.strip()), msg[:120])[:200]
        logger.warning("friend-bot error detected in %s: %s", unit, summary)

        if op_token and op_chat:
            dispatching = now - last_dispatch >= DISPATCH_GAP_S
            note = "Dispatching a fix agent now." if dispatching else "A fix agent is already working; not launching another."
            await send_message(
                op_token,
                op_chat,
                f"⚠️ Helper bot hit an error ({unit}):\n{summary}\n\n{note}",
            )

        if now - last_dispatch >= DISPATCH_GAP_S:
            _dispatch_fix_agent(unit, msg)
            last_dispatch = now

    state["seen"] = seen
    state["last_dispatch_ts"] = last_dispatch
    _save_state(state)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
