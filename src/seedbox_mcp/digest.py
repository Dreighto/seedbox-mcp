from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re

from fastmcp import Client

from seedbox_mcp.chat.ollama_ai import (
    ACTION_TOOLS,
    DEFAULT_OLLAMA_URL,
    ESCALATION_TOOLS,
    run_agent_turn,
)
from seedbox_mcp.config import Settings
from seedbox_mcp.graduation import graduation_nudge
from seedbox_mcp.telegram import send_message_html
from seedbox_mcp.triage import FINDINGS_INSTRUCTION, parse_findings, render_triage, save_run

logger = logging.getLogger("seedbox_mcp.digest")

# Cloud-tagged, flat-rate under the operator's Ollama Pro subscription — see
# reference: smaller/faster models for latency-sensitive replies, bigger ones
# for batch judgment calls where quality matters more than turnaround.
DEFAULT_DIGEST_MODEL = "deepseek-v4-pro:cloud"

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
you escalated it and why; don't just silently note the problem and move on. \
When you're reporting something that could be cleaned up (stale media, \
disk space) but you have no tool to act on it, describe the problem and \
either escalate it or leave it for the operator's own judgment — don't \
suggest a manual, ungated path (deleting files/folders directly, editing \
config by hand) as the fix, even as an aside. That's outsourcing an \
unaudited shortcut around the same safety rails your own tools have.

Write a short plain-English digest:
- Lead with anything that needs the operator's attention, including \
anything you fixed or escalated. If nothing does, say so plainly — do not \
invent urgency.
- Group the rest by area (backups / media / downloads / other storage).
- Be concrete: name the thing, the number, the age. "3 movies added 6+ \
months ago, never watched, 41GB" beats "some old content was found."
- Keep it under ~350 words unless something genuinely needs more detail.

Formatting: this renders in Telegram, which doesn't support markdown \
tables in any mode and only single *asterisks* make bold text (double \
**asterisks** show up as literal asterisks). Use short "Label: value" \
lines instead of a table.

Writing style:
- No em-dashes. Use a period, comma, or semicolon instead.
- No filler ("it's important to note") and no hedging ("may potentially"). \
Say the thing or don't say it.
- Before presenting two things as separate findings, check they're \
actually different. Don't restate the same fact twice in different words.
"""


class DigestSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_digest_model: str = DEFAULT_DIGEST_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


# fix_import is deliberately withheld from the scheduled digest: it mutates
# the library (adds a series/movie, triggers an import) and stays
# operator-driven (operator's call 2026-07-02). Enforced by omission, not by
# a prompt line the model could ignore — the digest reports a stuck import
# for the operator to fix interactively, it can't run the fix itself.
DIGEST_ACTION_TOOLS = ACTION_TOOLS - {"nasdoom_fix_import"}

# Context-bloat control, same rationale as monitor.MONITOR_READ_ONLY_TOOLS:
# the digest runs once a day with a FIXED job (DEFAULT_TASK below plus the
# SYSTEM_PROMPT's Tier 1 action-tool preambles), so it declares exactly the
# read tools that job needs rather than inheriting the whole READ_ONLY_TOOLS
# set. Inheriting the global set carried ~67 tool schemas (~11k tokens) on
# deepseek-v4-pro every run while DEFAULT_TASK calls 9 of them — this list
# decouples the digest from READ_ONLY_TOOLS' growth: a new read tool only
# reaches the digest if it's added HERE on purpose. Each entry is either
# called directly by DEFAULT_TASK, or is the read-side preview a Tier 1
# action tool needs (nasdoom_match_search -> nasdoom_match_apply, per
# SYSTEM_PROMPT's "Tier 1 action tools" section).
DIGEST_READ_ONLY_TOOLS: set[str] = {
    "fleet_health",
    "staleness_report",
    "nasdoom_health",
    "nasdoom_queue",
    "nasdoom_requests_overview",
    "nasdoom_control",
    "nas_backup_health",
    "nas_storage_inventory",
    "nas_import_diagnosis",
    "nasdoom_match_search",
}


async def run_digest(task: str, model: str | None = None) -> str:
    settings = DigestSettings()  # type: ignore[call-arg]
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    # No history — each scheduled run is a fresh report, not a continuation
    # of yesterday's. Multi-turn memory is a telegram_bot.py concept.
    text, _history, _pending_action, _known_entity_ids = await run_agent_turn(
        task + "\n\n" + FINDINGS_INSTRUCTION,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_digest_model,
        allowed_tools=DIGEST_READ_ONLY_TOOLS | DIGEST_ACTION_TOOLS | ESCALATION_TOOLS,
        action_tools=DIGEST_ACTION_TOOLS,
        ollama_url=settings.ollama_url,
    )
    return text


DEFAULT_TASK = (
    "Run the routine NAS health check: fleet_health (whole-cluster up/down "
    "for every node and service), staleness_report (movies+tv, "
    "older_than_days=120, include_missing=true), nasdoom_health, "
    "nasdoom_queue, nasdoom_requests_overview, nasdoom_control, "
    "nas_backup_health, nas_storage_inventory, and nas_import_diagnosis. If "
    "nas_import_diagnosis shows any downloads stuck on import, REPORT them by "
    "name so the operator can fix them (say they can ask you to fix a stuck "
    "import) — do NOT run nasdoom_fix_import yourself; it stays operator-"
    "driven. Summarize."
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run a one-shot NAS housekeeping digest.")
    parser.add_argument("--task", default=DEFAULT_TASK, help="Override the digest task prompt.")
    parser.add_argument("--model", default=None, help=f"Ollama model tag (default: {DEFAULT_DIGEST_MODEL}).")
    parser.add_argument("--no-telegram", action="store_true", help="Print only, skip the Telegram push.")
    args = parser.parse_args()

    result = asyncio.run(run_digest(args.task, args.model))

    findings = parse_findings(result)
    run_id = save_run(findings)
    rendered, markup = render_triage(findings, run_id=run_id)

    # Deterministic autonomy nudge appended out of the LLM's hands: when a fix
    # tool has earned graduation (or has failures worth a look), tell the
    # operator here rather than hoping the model notices. Silent otherwise.
    # Kept visually separate (own line, own emoji) from the health findings
    # above it — it's a different kind of thing (a governance question, not
    # a NAS status item) and reads confusingly folded into the same block.
    nudge = graduation_nudge()
    if nudge:
        nudge_html = re.sub(r"\*(.+?)\*", r"<b>\1</b>", html.escape(nudge))
        rendered = f"{rendered}\n\n\U0001f4ca {nudge_html}"

    print(rendered)

    if not args.no_telegram:
        settings = DigestSettings()  # type: ignore[call-arg]
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message_html(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    rendered,
                    reply_markup=markup,
                )
            )
        else:
            logger.warning(
                "Telegram not configured — set NAS_OPS_TELEGRAM_BOT_TOKEN + "
                "NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID in .env"
            )


if __name__ == "__main__":
    main()
