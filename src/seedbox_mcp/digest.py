from __future__ import annotations

import argparse
import asyncio
import logging

from fastmcp import Client

from seedbox_mcp.chat.ollama_ai import (
    ACTION_TOOLS,
    DEFAULT_OLLAMA_URL,
    ESCALATION_TOOLS,
    READ_ONLY_TOOLS,
    run_agent_turn,
)
from seedbox_mcp.config import Settings
from seedbox_mcp.telegram import send_message

logger = logging.getLogger("seedbox_mcp.digest")

# Cloud-tagged, flat-rate under the operator's Ollama Pro subscription — see
# reference: smaller/faster models for latency-sensitive replies, bigger ones
# for batch judgment calls where quality matters more than turnaround.
DEFAULT_DIGEST_MODEL = "qwen3-coder:480b-cloud"

SYSTEM_PROMPT = """\
You are a NAS housekeeping assistant. You run on a schedule, not in a chat — \
nobody is watching live, so write one self-contained report, not a conversation.

You have read-only tools covering the whole NAS. Prefer the nasdoom_* tools \
where they overlap with something else — they're the NASDOOM app's own BFF, \
already reconciled and denominator-aware, so using them keeps this digest \
consistent with what the operator sees in the app:
- nasdoom_health — one-call reachability rollup for all 8 services, instead \
of media_status + prowlarr_overview + sabnzbd_overview + jellyseerr_overview \
separately.
- nasdoom_queue — unified SABnzbd + arr-import queue in one view.
- nasdoom_requests_overview — friend-request state with human-readable labels.
- nasdoom_control — storage with a real percentFull denominator on the media \
pool (not a raw byte count).
Use staleness_report for library-wide unwatched/stale sweeps (NASDOOM has no \
equivalent), and nas_backup_health / nas_storage_inventory for backups and \
the non-media dirs (Music, samples, Transfer) — also NASDOOM has no coverage \
there.

You also have Tier 1 action tools — reversible, low-stakes fixes you should \
just apply when you find the matching problem, not just report:
- nasdoom_queue_command / nasdoom_queue_item_command — the queue is paused \
or an item is stuck: resume it, cancel it, or reprioritize it.
- nasdoom_requests_action — a request has been sitting pending a long time \
and is an obvious approve/decline call.
- nasdoom_match_search + nasdoom_match_apply — a Plex item is clearly \
mismatched and you can find the right match.
Each of these takes confirm=false|true. ALWAYS call with confirm=false \
first — it returns the current state and exactly what would change, \
without writing anything. Read that preview, then only call again with \
confirm=true if it's actually correct (e.g. the matched request/item in \
the preview is the one you meant). If you take one of these actions, you \
MUST say so explicitly in the report — name what was wrong and what you \
did about it. Never fix something silently. If you're not confident an \
action is correct (ambiguous case, matching not obviously right, a \
pending request that might be intentional, or the preview's "found" flag \
came back false), report it instead of guessing or forcing confirm=true.

For anything you find that's broken but outside these tools' reach — a \
failed backup, a disabled indexer, a service that's down, config drift — \
call escalate_to_worker with a clear description of what you found. That \
hands it to a full worker with real system access. Say in the report that \
you escalated it and why; don't just silently note the problem and move on.

Write a short plain-English digest:
- Lead with anything that needs the operator's attention, including \
anything you fixed or escalated. If nothing does, say so plainly — do not \
invent urgency.
- Group the rest by area (backups / media / downloads / other storage).
- Be concrete: name the thing, the number, the age. "3 movies added 6+ \
months ago, never watched, 41GB" beats "some old content was found."
- Keep it under ~350 words unless something genuinely needs more detail.
"""


class DigestSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_digest_model: str = DEFAULT_DIGEST_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def run_digest(task: str, model: str | None = None) -> str:
    settings = DigestSettings()  # type: ignore[call-arg]
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    # No history — each scheduled run is a fresh report, not a continuation
    # of yesterday's. Multi-turn memory is a telegram_bot.py concept.
    text, _history = await run_agent_turn(
        task,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_digest_model,
        allowed_tools=READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS,
        ollama_url=settings.ollama_url,
    )
    return text


DEFAULT_TASK = (
    "Run the routine NAS health check: staleness_report (movies+tv, "
    "older_than_days=120, include_missing=true), nasdoom_health, "
    "nasdoom_queue, nasdoom_requests_overview, nasdoom_control, "
    "nas_backup_health, and nas_storage_inventory. Summarize."
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run a one-shot NAS housekeeping digest.")
    parser.add_argument("--task", default=DEFAULT_TASK, help="Override the digest task prompt.")
    parser.add_argument("--model", default=None, help=f"Ollama model tag (default: {DEFAULT_DIGEST_MODEL}).")
    parser.add_argument("--no-telegram", action="store_true", help="Print only, skip the Telegram push.")
    args = parser.parse_args()

    result = asyncio.run(run_digest(args.task, args.model))
    print(result)

    if not args.no_telegram:
        settings = DigestSettings()  # type: ignore[call-arg]
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    result,
                )
            )
        else:
            logger.warning(
                "Telegram not configured — set NAS_OPS_TELEGRAM_BOT_TOKEN + "
                "NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID in .env"
            )


if __name__ == "__main__":
    main()
