from __future__ import annotations

import asyncio
import base64
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
# Per-chat value is {"history": [...], "pending_action": {...} | null,
# "known_entity_ids": {...}} — pending_action and known_entity_ids travel
# alongside history because they're the same kind of per-conversation state
# (see ollama_ai.run_agent_turn's params of the same names): the single
# action-tool preview currently confirmable, and the set of entity ids
# (tmdb_id, tvdb_id, ...) actually observed from real tool results so a
# later write can't reference an invented one. Losing either on restart just
# means an in-flight confirm/id-reference fails closed and needs a fresh
# preview/lookup — an ok-either-way default rather than a real problem.
HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / ".telegram_bot_history.json"


class ChatState(dict[str, Any]):
    """{"history": list[dict], "pending_action": dict | None,
    "known_entity_ids": dict[str, list[int]]} — a thin alias so callers
    don't have to spell out the shape every time."""


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
            # no pending action / no known ids rather than guessing from old data.
            states[key] = ChatState(history=value, pending_action=None, known_entity_ids={})
        elif isinstance(value, dict):
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
You are the operator's NAS Ops assistant, talking to them directly over \
Telegram. You have read-only tools covering the whole NAS: Plex, Radarr, \
Sonarr, Tautulli, backup health, and NAS storage outside the Plex library.

For "what are my internet speeds" / "how fast is the NAS's connection" — use \
nas_internet_speed_test. It runs a real test FROM the NAS itself (not from \
wherever this bot runs), takes ~5-10s, and always runs fresh — there's no \
cached result. It consumes real bandwidth (a few hundred MB), so don't call \
it more than once per conversation unless the operator specifically asks \
you to re-test.

For anything needing current outside information — an error message you \
don't recognize, current best practices, a new tool/integration worth \
considering for this NAS or the wider cluster — use web_search (and \
web_fetch to read a specific result in full). Don't guess or answer from \
possibly-stale training knowledge when a quick search would give a real \
answer. If the operator asks what could make the NAS/setup better, or you \
notice something (an outdated approach, a gap a well-known tool already \
solves) while answering something else, it's fine to proactively suggest \
it — just be clear it's a suggestion, not something you're about to do; \
actually adding new infrastructure is escalate_to_worker territory, not \
something you'd do yourself.

If the operator sends a photo of a movie/show poster or cover, OCR already \
ran on it before you saw this message — the task will tell you the \
extracted text, ranked by how prominent it was on the image. Use your own \
judgment on which line is actually the title (largest text usually is, but \
not always — ignore taglines, cast names, studio logos) before searching; \
say which text you're treating as the title so the operator can correct \
you if OCR misread something.

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

You can manage the Radarr/Sonarr library directly, not just monitor it:
- media_search(query) or nasdoom_omni_search(query) — find a title's \
tmdb_id/tvdb_id first, ALWAYS, before calling nasdoom_add. The harness \
itself rejects an id that didn't come from a real search result here — \
never state or guess a tmdb_id/tvdb_id from your own memory, even if you're \
confident you know it; if you do, the call gets blocked and you'll have to \
search anyway, so just search first.
- nasdoom_add — add a movie or series (kind: movie|tv). Leave \
quality_profile_id and root_folder_path unset unless the operator names a \
specific one — NASDOOM automatically routes anime to the anime \
folder/profile and everything else to the regular library default, which \
is right far more often than a single fixed default would be. Don't \
"upgrade" to a higher quality on your own judgment; the default is the \
default until the operator says otherwise. search_now defaults to false \
(adds/monitors without an immediate grab) — only set true if asked to grab \
it now, not just track it. If the operator wants to see quality options \
first, nasdoom_profiles(kind) lists them.
- radarr_research_movie / sonarr_research_series — fix something already in \
the library: search again, refresh metadata, or rescan a file already on \
disk. Use this before ever considering a delete/re-add cycle.
- sonarr_monitor_season — "add season N of X" when the show's already in \
Sonarr but that season isn't monitored yet.
- radarr_queue_action / sonarr_queue_action — unstick a queue item at the \
arr level (remove or blocklist), distinct from nasdoom_queue_item_command \
which covers NASDOOM's merged view; use whichever the operator's phrasing \
points at (mentions Radarr/Sonarr specifically vs. just "the queue").
- radarr_calendar / sonarr_calendar — "what's releasing/airing soon" for \
already-tracked titles.
- radarr_blocklist / sonarr_blocklist — see why something keeps failing to \
grab; radarr_blocklist_remove / sonarr_blocklist_remove un-blocks a release \
so it can be tried again (confirm-gated, reversible — can always \
re-blocklist via queue_action if it turns out to be bad again).
There is deliberately no delete tool in your reach — removing something \
from the library is a bigger, more irreversible decision than anything \
above, and isn't part of what you can do yet.

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

Formatting: this renders in Telegram, not a markdown viewer — it doesn't \
support tables in any mode, and only single *asterisks* make bold text \
(double **asterisks** show up as literal asterisks). Never use a markdown \
table; use short "Label: value" lines instead, one per line, grouped with \
a blank line between sections if there are several. Keep formatting light \
— this is a chat message, not a document.
"""

# Answered directly, without going through the model, when the message is
# exactly "/help" or "/start" (checked before run_agent_turn in
# _handle_message). Deterministic and instant: capability questions don't
# need a model call, and a model-generated capability list risks describing
# a tool that doesn't exist or omitting one that does. Keep this in sync by
# hand whenever READ_ONLY_TOOLS/ACTION_TOOLS gains or loses a tool — it's a
# curated summary for a human, not a generated dump of the tool schema.
HELP_TEXT = """\
I'm the NAS Ops assistant — ask me anything in plain English, no commands \
needed except this one. Here's what I can actually do:

📊 Status & health
Plex, Radarr, Sonarr, Prowlarr, SABnzbd, Jellyseerr reachability · backup \
health · NAS storage (Music/samples/Transfer) · a real internet speed test \
run from the NAS itself · Prowlarr per-indexer failure stats · library \
staleness sweeps ("what haven't I watched in months")

🎬 Library — Radarr/Sonarr
Search and check what's already tracked · add a movie or series (auto-\
routes anime vs regular content, uses your default quality unless you name \
one) · what's releasing/airing soon · fix a stuck item (re-search, refresh, \
rescan) · add a missing season · see and un-block a failed/blocklisted \
release · unstick a queue item · send a photo of a poster/cover and I'll \
OCR it and check if it's already in the library

📥 Downloads & requests
Unified download queue (pause/resume/speedcap/cancel/reprioritize) · \
approve or decline a pending Jellyseerr request · fix a mismatched Plex item

🎵 Everything else (music samples, software, games, books)
Search Prowlarr directly and download — the arrs don't cover this, this \
does

🌐 Web
Search the live web and read a page's full content — for anything outside \
this NAS's own state (error messages, current best practices, evaluating a \
new tool for the setup)

🔗 Friend file-share portal (files.logueos.xyz)
List/create/revoke friend accounts (download-only or upload-enabled) · see \
what's shared

Anything with real consequences (adding content, changing settings, \
downloading) always previews first and asks before doing it for real — I'll \
never just go do something without showing you what's about to happen.

Anything genuinely broken that I can't fix myself — a service down, a \
config problem — I hand off to a full worker with real system access and \
tell you I did it.

Not built yet, on purpose: deleting anything from the library, general \
storage cleanup. Those need a stronger safety pattern than what I have \
right now.
"""


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
        reply, new_history, new_pending_action, new_known_entity_ids = await run_agent_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            mcp_client=mcp_client,
            model=settings.ollama_bot_model,
            allowed_tools=READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS,
            history=state.get("history", []),
            pending_action=state.get("pending_action"),
            known_entity_ids=state.get("known_entity_ids"),
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
    return ChatState(
        history=trim_history(new_history), pending_action=new_pending_action, known_entity_ids=new_known_entity_ids
    )


async def _download_telegram_photo(token: str, file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=20.0) as http:
        resp = await http.get(f"{TELEGRAM_API}/bot{token}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        resp = await http.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        resp.raise_for_status()
        return resp.content


async def _handle_photo_message(
    settings: BotSettings, token: str, chat_id: int, photo_sizes: list[dict[str, Any]], caption: str, state: ChatState
) -> ChatState:
    """Downloads the photo, runs OCR deterministically (the model has no way
    to fetch Telegram file bytes itself — this is the same "do the
    mechanical part in code, hand the LLM only what needs judgment" pattern
    as monitor.py's queue-resume check), then routes the extracted text
    through the normal _handle_message pipeline so the model uses its usual
    search/add tools and confirm-gates on the result."""
    largest = max(photo_sizes, key=lambda p: p.get("file_size") or (p.get("width", 0) * p.get("height", 0)))
    try:
        image_bytes = await _download_telegram_photo(token, largest["file_id"])
    except httpx.HTTPError:
        logger.exception("failed to download photo from Telegram")
        await send_message(token, chat_id, "Couldn't download that photo from Telegram — try sending it again?")
        return state

    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    try:
        async with mcp_client:
            result = await mcp_client.call_tool("poster_ocr", {"image_b64": base64.b64encode(image_bytes).decode()})
        ocr_text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
        ocr_data = json.loads(ocr_text).get("data", {})
    except Exception:
        logger.exception("poster OCR failed")
        await send_message(token, chat_id, "Couldn't read text from that photo — the OCR service may be down.")
        return state

    texts = ocr_data.get("texts_by_prominence", [])
    if not texts:
        await send_message(
            token, chat_id, "Didn't find any readable text in that photo — is it a clear shot of the poster/cover?"
        )
        return state

    extracted = "; ".join(f'"{t["text"]}" (confidence {t.get("confidence", 0):.2f})' for t in texts[:8])
    task = (
        f"The operator sent a photo. OCR extracted this text, largest/most prominent first: {extracted}. "
        f"{'They also wrote: ' + caption if caption else ''} "
        "Figure out the likely movie/show title (the largest text is usually it, but use judgment — "
        "ignore taglines, cast names, studio logos), then check whether it's already in the Plex library "
        "or could be added (media_search / nasdoom_omni_search). Report what you found in plain terms, and "
        "if it's not in the library yet, offer to add it — don't add it without the operator confirming, "
        "same as any other add."
    )
    return await _handle_message(settings, token, chat_id, task, state)


async def run_bot() -> None:
    settings = BotSettings()  # type: ignore[call-arg]
    if not settings.nas_ops_telegram_bot_token or not settings.nas_ops_telegram_allowed_chat_id:
        raise SystemExit("NAS_OPS_TELEGRAM_BOT_TOKEN and NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID must be set in .env")
    token = settings.nas_ops_telegram_bot_token.get_secret_value()
    allowed_chat_id = settings.nas_ops_telegram_allowed_chat_id

    await _set_bot_commands(token)
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
                photo = message.get("photo")
                if chat.get("id") != allowed_chat_id:
                    logger.warning("ignored message from unauthorized chat_id=%s", chat.get("id"))
                    continue
                if photo:
                    logger.info("photo message: %d size(s)", len(photo))
                    state = chat_states.get(
                        allowed_chat_id, ChatState(history=[], pending_action=None, known_entity_ids={})
                    )
                    chat_states[allowed_chat_id] = await _handle_photo_message(
                        settings, token, allowed_chat_id, photo, message.get("caption") or "", state
                    )
                    _save_chat_states(chat_states)
                    continue
                if not text:
                    continue
                logger.info("message: %r", text)
                if text.strip().split()[0].split("@")[0] in ("/help", "/start"):
                    # Deterministic, no model call — see HELP_TEXT's own
                    # comment for why this bypasses the LLM entirely.
                    await send_message(token, allowed_chat_id, HELP_TEXT)
                    continue
                state = chat_states.get(
                    allowed_chat_id, ChatState(history=[], pending_action=None, known_entity_ids={})
                )
                chat_states[allowed_chat_id] = await _handle_message(settings, token, allowed_chat_id, text, state)
                _save_chat_states(chat_states)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
