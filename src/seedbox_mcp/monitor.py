from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client

from seedbox_mcp.action_audit import rate_limit_exceeded, record_action
from seedbox_mcp.chat.ollama_ai import DEFAULT_OLLAMA_URL, KEEP_ALIVE, READ_ONLY_TOOLS, run_agent_turn
from seedbox_mcp.config import Settings
from seedbox_mcp.download_strikes import run_download_strike_check
from seedbox_mcp.telegram import send_message
from seedbox_mcp.telegram_bot import DEFAULT_BOT_MODEL

logger = logging.getLogger("seedbox_mcp.monitor")

# The digest's bigger, slower model, not the interactive bot's fast/cheap
# one — live testing found gpt-oss:20b-cloud inconsistent on this specific
# job: a multi-step autonomous fix (confirm=false preview -> confirm=true
# execute) sometimes completed cleanly and sometimes stopped after the
# preview with no confirm=true, silently leaving the real problem
# unfixed. Nobody's waiting live on a background cycle the way they are on
# an interactive reply, so correctness matters more than the few seconds of
# latency difference — same tradeoff logic as digest.py's own model choice.
DEFAULT_MONITOR_MODEL = "qwen3-coder:480b-cloud"

# Deliberately the ORIGINAL Tier 1 set only — not everything ACTION_TOOLS now
# covers. Operator's own call (2026-07-01): the monitor may act autonomously
# on these without a human in the loop (matches digest.py's existing
# authority), but today's newer additions (library add, blocklist-remove,
# arr-level queue_action, web_search) stay interactive-only for now — "the
# monitor needing to be interactive would require several steps for simple
# or routine tasks... the other new tools can be driven manually." Expected
# to grow over time as specific tools prove themselves necessary for
# unattended operation — extend deliberately, not by just unioning with
# ACTION_TOOLS wholesale.
MONITOR_ACTION_TOOLS: set[str] = {
    "nasdoom_queue_command",
    "nasdoom_queue_item_command",
    "nasdoom_requests_action",
    "nasdoom_match_apply",
}
MONITOR_ESCALATION_TOOLS: set[str] = {"escalate_to_worker"}
# web_search stays out per the same operator decision above.
MONITOR_READ_ONLY_TOOLS: set[str] = READ_ONLY_TOOLS - {"web_search", "web_fetch"}

# The model outputs exactly this (nothing else) when a cycle finds nothing
# worth surfacing — checked literally by main() to decide whether to push to
# Telegram at all. Unlike digest.py, which always reports something (it's a
# scheduled daily summary the operator expects to see), most monitor cycles
# should be silent — pinging on every routine "all clear" defeats the
# "truly important" bar the operator asked for and trains the operator to
# ignore the channel.
NO_ALERT_SENTINEL = "NO_ALERT_NEEDED"

SYSTEM_PROMPT = f"""\
You are a frequent, lightweight NAS health monitor — you run every 30 \
minutes, not once a day like the full digest. Almost every run should end \
with nothing to report; you exist to catch things BETWEEN the operator's \
own checks and the daily digest, not to generate routine chatter.

Check: nasdoom_health, nasdoom_queue, nasdoom_requests_overview, \
nasdoom_control, nas_backup_health, prowlarr_indexer_stats.

The queue is the one thing here you should fix on sight, no second-guessing: \
if nasdoom_queue shows paused=true, that's it — call \
nasdoom_queue_command(action=resume, confirm=false) then confirm=true \
immediately, same turn, no exceptions, no waiting for a second reading. \
This is fully reversible (pausing it back takes one call), so there's no \
reason to hesitate the way there is for something you'd page the operator \
about. Same directness for a genuinely stuck queue item \
(nasdoom_queue_item_command) or an obvious approve/decline on a request \
that's been sitting a long time (nasdoom_requests_action), or a clearly \
mismatched Plex item with an obvious correct match (nasdoom_match_apply). \
If you fix any of these, that outcome belongs in your report — say what \
was wrong and what you did, even though nobody asked you to.

Everything else needs real verification before you treat it as worth \
flagging to the operator — this is the "not a false flag" bar. A single \
reading of "down" or "failed" is not enough on its own to justify paging \
someone; a service can blip and recover in the time it takes to check \
twice. Before treating one of THESE as real:
- For a service reachability issue: call the same health check again once \
more in this same turn before deciding it's real, not a transient blip.
- For backup health: only "failed" or genuinely stale-beyond-threshold \
counts — nas_backup_health already applies the right threshold, trust its \
status field, don't apply your own looser bar.
- For an indexer: only nonzero failed_auth_queries, or a >50% query \
failure rate (prowlarr_indexer_stats' own likely_needs_attention flag) — \
not a single failed query, that's normal noise.
- For storage: only genuinely high (>90%) media pool usage, not "getting \
fuller than usual".
If you can't get a second reading to confirm within this same turn (a tool \
error prevents re-checking), say so explicitly rather than either \
suppressing it or reporting it as confirmed — an "I couldn't verify this, \
here's why" is a legitimate, honest outcome, not a failure.

For anything broken that's beyond these tools' reach — call \
escalate_to_worker, then report that you escalated it and why.

You do NOT have access to library-add, blocklist-management, arr-level \
queue actions, or web search on this scheduled path — if something would \
need one of those, that's exactly the kind of thing to escalate or report \
for the operator's own judgment, not attempt to work around.

If — after applying the verification rules above — there is genuinely \
nothing that needs the operator's attention this cycle (everything you \
checked came back healthy, or a blip you caught self-resolved on the \
second reading), your ENTIRE response must be exactly this and nothing \
else: {NO_ALERT_SENTINEL}
Do not pad a real "all clear" into any other text, and do not use this \
sentinel if you actually found or fixed something — that always gets a \
real report instead, even if what you fixed was minor.

Formatting (when you do write a report): this renders in Telegram, which \
doesn't support markdown tables in any mode and only single *asterisks* \
make bold text (double **asterisks** show up as literal asterisks). Use \
short "Label: value" lines instead of a table.

Writing style: no em-dashes (use a period, comma, or semicolon instead), \
no filler or hedging, no restating the same finding twice in different \
words.
"""


class MonitorSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_monitor_model: str = DEFAULT_MONITOR_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def _keep_interactive_model_warm(ollama_url: str) -> None:
    """Trivial no-tools ping to the INTERACTIVE bot's model (not this
    monitor's own, bigger one) with a long keep_alive — piggybacks on this
    cycle's existing 30-min cadence so gpt-oss:20b-cloud stays resident
    through normal operating hours instead of only getting warmed by
    whenever the operator happens to message next. Best-effort: a failure
    here shouldn't fail the actual monitor cycle."""
    try:
        async with httpx.AsyncClient(base_url=ollama_url, timeout=30.0) as http:
            await http.post(
                "/api/chat",
                json={
                    "model": DEFAULT_BOT_MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                },
            )
    except httpx.HTTPError:
        logger.warning("keep-warm ping for %s failed (non-fatal)", DEFAULT_BOT_MODEL, exc_info=True)


async def _deterministic_queue_resume(mcp_client: Client[Any]) -> str | None:
    """Checks nasdoom_queue and resumes it directly (no LLM call) if paused.
    Returns a note for the report if it acted, None otherwise.

    This exists because live testing found the LLM path for this exact case
    fail twice, with two different models: the model would read
    nasdoom_queue, see paused=true, decide in its own reasoning to fix it,
    and then simply never call nasdoom_queue_command — followed by a final
    report claiming "I have resumed it" anyway, a flatly false claim with no
    real action behind it. "Queue paused -> resume it" needs zero judgment
    (unlike the backup/indexer/storage checks, which genuinely need
    interpretation), so it doesn't need an LLM in the loop at all — doing it
    here in code removes the failure mode entirely rather than trying to
    prompt-engineer around a model that already demonstrated it can silently
    skip the one tool call that mattered."""
    if rate_limit_exceeded():
        return None
    async with mcp_client:
        result = await mcp_client.call_tool("nasdoom_queue", {})
    try:
        data = json.loads("\n".join(b.text for b in result.content if hasattr(b, "text")))
    except json.JSONDecodeError:
        return None
    paused = ((data.get("data") or {}).get("global") or {}).get("paused")
    if not paused:
        return None
    args = {"action": "resume", "confirm": True}
    async with mcp_client:
        await mcp_client.call_tool("nasdoom_queue_command", args)
    record_action("nasdoom_queue_command", args, dry_run=False, outcome="ok")
    logger.info("monitor: deterministically resumed a paused queue")
    return "Deterministic check: the download queue was paused — resumed it automatically."


async def run_monitor_cycle(model: str | None = None) -> str | None:
    """Returns the alert text, or None if the cycle found nothing worth
    surfacing (the sentinel case) — None is the expected, common outcome."""
    settings = MonitorSettings()  # type: ignore[call-arg]
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())

    await _keep_interactive_model_warm(settings.ollama_url)
    queue_fix_note = await _deterministic_queue_resume(mcp_client)
    # Strike-based stalled-download fixer — deterministic, same "keep it out
    # of the LLM's hands" rationale as the queue-resume above: strike
    # counting needs persistent state across cycles and a fixed threshold,
    # not model judgment. Runs before the LLM and its outcome is folded into
    # the report.
    strike_note = None
    try:
        strike_note = await run_download_strike_check(settings, time.time())
    except Exception:
        logger.exception("download strike check failed (non-fatal)")

    # Spelled out as an explicit checklist in the TASK message, not just
    # buried in the system prompt — live testing found the model skip
    # nasdoom_queue entirely in a cycle where it was the one tool with an
    # actual finding, most likely because a run of hallucinated-kwarg
    # retries on other tools ate the round budget first. A direct per-turn
    # checklist is a stronger nudge than a system-prompt line the model has
    # to recall unprompted.
    task = (
        "Run your check cycle now. Call each of these exactly once, in this order, "
        "before deciding whether anything needs a report: nasdoom_health, nasdoom_queue, "
        "nasdoom_requests_overview, nasdoom_control, nas_backup_health, prowlarr_indexer_stats, "
        "nas_disk_health. Disk verdicts (ok/watch/replace_now) are computed in code; a watch or "
        "replace_now verdict is ALWAYS report-worthy, quoted with its exact reasons. "
        "Note: the queue's paused state AND any stalled downloads have already been checked and "
        "auto-corrected before you started, by separate deterministic steps — don't try to fix "
        "those yourself, just report the queue's current state like any other check."
    )
    text, _history, _pending_action, _known_entity_ids = await run_agent_turn(
        task,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_monitor_model,
        allowed_tools=MONITOR_READ_ONLY_TOOLS | MONITOR_ACTION_TOOLS | MONITOR_ESCALATION_TOOLS,
        action_tools=MONITOR_ACTION_TOOLS,
        escalation_tools=MONITOR_ESCALATION_TOOLS,
        ollama_url=settings.ollama_url,
        # Six independent signals to check in one turn, each a real chance
        # to consume a round on a hallucinated-then-retried kwarg (an
        # observed live pattern) — the default budget is tuned for a
        # 1-2-tool interactive reply, not a full sweep. Bumped from an
        # initial 14 after a live run still burned through it on kwarg
        # retries before reaching every check.
        max_tool_rounds=20,
    )
    llm_alert = None if text.strip() == NO_ALERT_SENTINEL else text
    # Deterministic-fix notes always surface (they describe real actions
    # taken or real import problems flagged), even on an otherwise-silent
    # cycle where the LLM returned the no-alert sentinel.
    parts = [p for p in (queue_fix_note, strike_note, llm_alert) if p]
    return "\n\n".join(parts) if parts else None


# Alert-dedup state: the fingerprint of the last alert we actually pushed,
# so an unresolved issue (e.g. an import block waiting on the operator) isn't
# re-pushed every 30-min cycle. A genuinely persistent alert is re-sent at
# most once per this interval as a reminder; a NEW or CHANGED alert always
# pushes immediately; a clear cycle (nothing to report) resets the state so
# a later recurrence re-alerts.
ALERT_STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".monitor_alert_state.json"
REMIND_INTERVAL_S = 12 * 3600


def _alert_decision(
    fingerprint: str | None, state: dict[str, Any], now_ts: float, remind_s: float = REMIND_INTERVAL_S
) -> tuple[bool, dict[str, Any]]:
    """Pure. (should_push, new_state) given the current alert fingerprint
    (None = nothing to report) and the persisted last-push state."""
    if fingerprint is None:
        return False, {}  # nothing active — reset
    if state.get("hash") != fingerprint:
        return True, {"hash": fingerprint, "last_pushed_ts": now_ts}  # new/changed
    # Same alert as last time — only re-remind after the interval.
    if now_ts - state.get("last_pushed_ts", 0) >= remind_s:
        return True, {"hash": fingerprint, "last_pushed_ts": now_ts}
    return False, state  # suppress the duplicate


def _load_alert_state() -> dict[str, Any]:
    if not ALERT_STATE_PATH.exists():
        return {}
    try:
        loaded = json.loads(ALERT_STATE_PATH.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_alert_state(state: dict[str, Any]) -> None:
    try:
        ALERT_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logger.exception("failed to persist alert state to %s", ALERT_STATE_PATH)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run one NAS monitor cycle; push to Telegram only if alert-worthy.")
    parser.add_argument("--model", default=None, help=f"Ollama model tag (default: {DEFAULT_MONITOR_MODEL}).")
    parser.add_argument("--no-telegram", action="store_true", help="Print only, skip the Telegram push.")
    parser.add_argument("--force-alert-test", action="store_true", help="Skip the sentinel check, always push.")
    args = parser.parse_args()

    result = asyncio.run(run_monitor_cycle(args.model))
    if result is None:
        print(f"[{NO_ALERT_SENTINEL}] nothing to report this cycle")
        # Nothing active — clear the dedup state so a later recurrence of the
        # same issue alerts fresh instead of being suppressed as a duplicate.
        _save_alert_state({})
        if not args.force_alert_test:
            return
        result = "(--force-alert-test) monitor cycle completed with no alert-worthy findings."
    else:
        print(result)

    # Alert-once-then-quiet: don't re-push an identical unresolved alert every
    # cycle. --force-alert-test bypasses the dedup (it's a manual test).
    if not args.force_alert_test:
        fingerprint = hashlib.sha256(result.encode()).hexdigest()
        should_push, new_state = _alert_decision(fingerprint, _load_alert_state(), time.time())
        _save_alert_state(new_state)
        if not should_push:
            print("[suppressed] same alert already pushed; staying quiet until it changes or the remind interval")
            return

    if not args.no_telegram:
        settings = MonitorSettings()  # type: ignore[call-arg]
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    f"🔔 {result}",
                )
            )
        else:
            logger.warning(
                "Telegram not configured — set NAS_OPS_TELEGRAM_BOT_TOKEN + "
                "NAS_OPS_TELEGRAM_ALLOWED_CHAT_ID in .env"
            )


if __name__ == "__main__":
    main()
