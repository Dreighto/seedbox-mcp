from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client

from seedbox_mcp.action_audit import rate_limit_exceeded, record_action
from seedbox_mcp.chat.ollama_ai import DEFAULT_OLLAMA_URL, KEEP_ALIVE, run_agent_turn
from seedbox_mcp.config import Settings
from seedbox_mcp.download_strikes import run_download_strike_check
from seedbox_mcp.quality_guard import run_quality_guard
from seedbox_mcp.telegram import send_message_html
from seedbox_mcp.telegram_bot import DEFAULT_BOT_MODEL
from seedbox_mcp.tools.host_health import AUTO_RECOVER_SERVICES
from seedbox_mcp.triage import (
    FINDINGS_INSTRUCTION,
    Finding,
    fingerprint,
    parse_findings,
    render_triage,
    save_run,
    slugify,
)

logger = logging.getLogger("seedbox_mcp.monitor")

# The digest's bigger, slower model, not the interactive bot's fast/cheap
# one — live testing found gpt-oss:20b-cloud inconsistent on this specific
# job: a multi-step autonomous fix (confirm=false preview -> confirm=true
# execute) sometimes completed cleanly and sometimes stopped after the
# preview with no confirm=true, silently leaving the real problem
# unfixed. Nobody's waiting live on a background cycle the way they are on
# an interactive reply, so correctness matters more than the few seconds of
# latency difference — same tradeoff logic as digest.py's own model choice.
DEFAULT_MONITOR_MODEL = "deepseek-v4-pro:cloud"

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

# Context-bloat control: the monitor runs every 30 min and has a FIXED job
# (the check list in SYSTEM_PROMPT), so it declares exactly the read tools it
# needs rather than inheriting the whole READ_ONLY_TOOLS set. Inheriting the
# global set carried ~48 tool schemas (~7k tokens) per cycle while using ~12 —
# 73% waste, and it grew every time a new read tool landed anywhere in the
# server (adguard_stats, nas_resources, etc. were all being dragged in). This
# explicit list decouples the monitor from that growth: a new tool only
# reaches the monitor if it's added HERE on purpose. Each entry is used by the
# prompt's check list or feeds one of the action tools (match_search ->
# match_apply, queue -> queue_item_command).
MONITOR_READ_ONLY_TOOLS: set[str] = {
    "fleet_health",
    "nasdoom_health",
    "nasdoom_queue",
    "nasdoom_requests_overview",
    "nasdoom_control",
    "nas_backup_health",
    "prowlarr_indexer_stats",
    # the run-cycle task EXPLICITLY orders a disk check (verdicts computed in
    # code, watch/replace_now always report-worthy) — it was missing from this
    # set, so every model was told to call a tool it didn't have. qwen skipped
    # it silently for days; V4-Pro honestly reported the mismatch (breaking the
    # exact-match NO_ALERT sentinel), which is how this surfaced.
    "nas_disk_health",
    "nasdoom_match_search",
    # so the LLM can see a container's actual state when deciding whether a
    # persistent (already-restarted) outage needs escalation.
    "nas_service_status",
}

# Tools the monitor can use WITHOUT an LLM in the loop, via deterministic
# pre-checks (queue resume, service recovery). Restarts stay code-driven —
# never LLM discretion (same rationale as _deterministic_queue_resume) — but
# they ARE autonomous now, so the graduation gate counts them as such.
# nas_service_restart GRADUATED here 2026-07-02 after clearing the gate:
# 7 recent clean restarts across 5 distinct services, 0 failures.
MONITOR_DETERMINISTIC_TOOLS: set[str] = {"nasdoom_queue_command", "nas_service_restart"}

# Don't re-restart the same service within this window: if a restart didn't
# make it stick, restarting again every cycle is a loop, not a fix — leave it
# down and let the LLM escalate instead.
RESTART_COOLDOWN_S = 2 * 3600
MONITOR_RESTART_STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".monitor_restart_state.json"

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

Check: fleet_health (whole-cluster up/down for every node and service in one \
call — the widest net, catches a node or non-media service being down), \
nasdoom_health, nasdoom_queue, nasdoom_requests_overview, nasdoom_control, \
nas_backup_health, prowlarr_indexer_stats. Treat a fleet_health "down" the \
same as any other reachability issue below: confirm with a second reading \
before flagging, since a single missed ping can be a blip.

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
- For a media request (nasdoom_requests_overview): a request sitting in \
'processing' (or 'searching') is normal and expected — it waits there until \
a downloadable release actually exists. NEVER flag one based on how long it \
has been processing, the request date, or a theatrical / in-cinemas date; \
none of those is the digital-release date, and elapsed time alone is not \
evidence of a problem. A title still in cinemas, or with no digital release \
yet, is correctly waiting, not stuck. Genuinely stalled downloads are \
already caught by the deterministic queue and strike checks, so a \
'processing' request is only worth mentioning if the queue/control view \
shows the title actually downloading and failing. Otherwise requests are \
healthy — do not report them.
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


def _load_restart_state() -> dict[str, float]:
    try:
        raw = json.loads(MONITOR_RESTART_STATE_PATH.read_text())
        return {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def _save_restart_state(state: dict[str, float]) -> None:
    try:
        MONITOR_RESTART_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logger.exception("failed to persist restart state to %s", MONITOR_RESTART_STATE_PATH)


def _extract(result: Any) -> dict[str, Any]:
    try:
        return dict(json.loads("\n".join(b.text for b in result.content if hasattr(b, "text"))))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


async def _deterministic_service_recovery(mcp_client: Client[Any], now_ts: float) -> str | None:
    """Restart a down media-stack container directly (no LLM), once per
    RESTART_COOLDOWN_S. GRADUATED autonomous action, kept deterministic for
    the same reason as the queue resume: "container is stopped -> restart it"
    needs no judgment, and leaving it to the LLM reintroduces the silent-skip
    failure mode. The per-service cooldown is the loop guard — if a restart
    didn't make it stick, we don't keep hammering it every cycle; we leave it
    down and flag it so the LLM cycle escalates instead."""
    if rate_limit_exceeded():
        return None
    async with mcp_client:
        status = await mcp_client.call_tool("nas_service_status", {})
    services = (_extract(status).get("data") or {}).get("services") or []
    down = [
        s
        for s in services
        if s.get("name") in AUTO_RECOVER_SERVICES and s.get("state") not in ("running", "restarting")
    ]
    if not down:
        return None
    state = _load_restart_state()
    notes: list[str] = []
    for svc in down:
        name = svc["name"]
        if now_ts - state.get(name, 0.0) < RESTART_COOLDOWN_S:
            mins = int((now_ts - state.get(name, 0.0)) / 60)
            notes.append(
                f"{name} is still down after a restart {mins} min ago — not restarting again "
                "(loop guard); this needs escalation."
            )
            continue
        args = {"name": name, "confirm": True}
        async with mcp_client:
            r = await mcp_client.call_tool("nas_service_restart", args)
        rd = _extract(r)
        verified = (rd.get("data") or {}).get("verified_running")
        ok = bool(rd.get("ok")) and verified is not False
        record_action(
            "nas_service_restart", args, dry_run=False,
            outcome="ok" if ok else f"failed: verified_running={verified}",
        )
        state[name] = now_ts
        logger.info("monitor: deterministically restarted %s (verified=%s)", name, verified)
        notes.append(
            f"{name} container was down — restarted it, verified back up."
            if ok
            else f"{name} container was down — restart did NOT bring it back; needs escalation."
        )
    _save_restart_state(state)
    return "\n".join(notes) if notes else None


# Markers for lines that must alert a human — checked FIRST, so a bundled
# note that contains both a real fix and a real problem (e.g. the strike
# checker's single joined note: "auto-fixed the stalled ones.\nN stuck on
# import, NOT auto-fixed ... Worth a look.") never has its unresolved half
# swallowed by the fix classification below.
_ATTENTION_MARKERS = (
    "not auto-fixed",
    "worth a look",
    "needs escalation",
    "did not",
    "still down",
    "loop guard",
    "approaching",
    "stuck",
)

# Markers for lines describing a deterministic fix that actually worked.
# Only used when a line matches NONE of the attention markers above.
_FIX_MARKERS = (
    "resumed it automatically",
    "auto-fixed stalled downloads",
    "restart succeeded",
    "back up",
    "verified",
    "auto-corrected bad imports",
    "added a permanent",
)


def _finding_title(line: str) -> str:
    """First sentence of the line, cut at the last whitespace before ~70
    chars rather than mid-word, with trailing punctuation trimmed."""
    first_sentence = line.split(".")[0].strip()
    if len(first_sentence) <= 70:
        return first_sentence.rstrip(", ")
    cut = first_sentence[:70].rsplit(" ", 1)[0]
    return cut.rstrip(", ")


def _notes_to_findings(*notes: str | None) -> list[Finding]:
    """Each deterministic-fix note (queue resume, strike fix, service restart)
    can bundle multiple lines — e.g. the strike checker joins a real fix and
    an unresolved report with "\\n" in one note. Each non-empty line becomes
    its OWN finding, classified line-by-line: a line is only ever marked
    auto_fixed (and so excluded from the push fingerprint) when it matches a
    fix marker and no attention marker. Anything else defaults to
    needs_fix/auto_fixed=False — pushing when unsure, rather than silently
    swallowing an unresolved problem bundled alongside a real fix."""
    out: list[Finding] = []
    for note in notes:
        if not note:
            continue
        for line in note.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            title = _finding_title(stripped)
            lowered = stripped.lower()
            has_attention = any(marker in lowered for marker in _ATTENTION_MARKERS)
            is_fix = not has_attention and any(marker in lowered for marker in _FIX_MARKERS)
            if is_fix:
                out.append(
                    Finding(
                        id=slugify(title),
                        severity="watch",
                        title=title,
                        real=True,
                        reason=stripped,
                        fixable_by="proven",
                        auto_fixed=True,
                    )
                )
            else:
                out.append(
                    Finding(
                        id=slugify(title),
                        severity="needs_fix",
                        title=title,
                        real=True,
                        reason=stripped,
                        recommendation="look into it",
                        fixable_by="agent",
                        auto_fixed=False,
                    )
                )
    return out


# Shared checklist fragment for both the scheduled (acting) and on-demand
# (read_only) task strings — kept as ONE fragment so the two paths can't
# silently drift apart on which tools get checked or in what order. Only the
# preamble sentence describing what already happened (or didn't) differs
# between the two callers below.
_CHECK_ORDER = (
    "Call each of these exactly once, in this order, before deciding whether anything needs a "
    "report: nasdoom_health, nasdoom_queue, nasdoom_requests_overview, nasdoom_control, "
    "nas_backup_health, prowlarr_indexer_stats, nas_disk_health. Disk verdicts (ok/watch/"
    "replace_now) are computed in code; a watch or replace_now verdict is ALWAYS report-worthy, "
    "quoted with its exact reasons."
)

# The scheduled path: deterministic fixers already ran before this turn, so
# the model is told not to duplicate that work and just report current
# state, plus the one thing that IS still its job (escalating a persistent
# failure the loop guard caught).
_ACTING_PREAMBLE = (
    f"Run your check cycle now. {_CHECK_ORDER} "
    "Note: the queue's paused state, any stalled downloads, AND any down media-stack "
    "container have already been checked and auto-corrected before you started, by separate "
    "deterministic steps — don't try to fix those yourself, just report current state. One "
    "thing that IS yours: if a service shows down/unhealthy and the recovery note says it was "
    "already restarted and didn't come back (loop guard tripped), that's a persistent failure "
    "— escalate_to_worker with what you see."
)

# The on-demand "full status" path: nothing has been auto-corrected before
# this turn (no deterministic fixers ran, and the model has no action or
# escalation tools available), so it must never claim otherwise — it only
# checks and reports current state.
_READ_ONLY_PREAMBLE = (
    f"Run a read-only status check now. {_CHECK_ORDER} "
    "You have read-only tools only for this request — nothing has been auto-corrected before "
    "you started, and you cannot fix or escalate anything yourself right now. Just report "
    "current state honestly, including anything that looks wrong (e.g. a paused queue, a stuck "
    "download, or a down service) exactly as you observe it."
)


async def run_monitor_cycle(model: str | None = None, read_only: bool = False) -> list[Finding]:
    """Runs one check cycle and returns the structured findings for it.
    An empty list means a clean cycle (nothing actionable) — the expected,
    common outcome. Deterministic-fix notes (queue resume, strike fix,
    service restart) are always folded in as auto-fixed findings, even on an
    otherwise-clean cycle.

    `read_only=True` is the on-demand "full status" path (Telegram status
    intent): it must never act. It skips all three deterministic fixers
    below and gives the model only read tools — no action_tools, no
    escalation_tools — so an operator asking "what's the status" can never
    trigger a real restart or queue change as a side effect. The scheduled
    path (`read_only=False`, the default) is unchanged: it's the only one
    with standing authority to act autonomously."""
    settings = MonitorSettings()  # type: ignore[call-arg]
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())

    await _keep_interactive_model_warm(settings.ollama_url)

    queue_fix_note = None
    strike_note = None
    recovery_note = None
    quality_guard_note = None
    if not read_only:
        queue_fix_note = await _deterministic_queue_resume(mcp_client)
        # Strike-based stalled-download fixer — deterministic, same "keep it
        # out of the LLM's hands" rationale as the queue-resume above:
        # strike counting needs persistent state across cycles and a fixed
        # threshold, not model judgment. Runs before the LLM and its outcome
        # is folded into the report.
        try:
            strike_note = await run_download_strike_check(settings, time.time())
        except Exception:
            logger.exception("download strike check failed (non-fatal)")

        # Post-import quality re-validation (deterministic, same rationale):
        # Radarr/Sonarr's profile "allowed" quality gate isn't re-checked
        # once the real file is inspected on import, so a mislabeled release
        # can land as BR-DISK/Remux/etc despite the profile disallowing it.
        # Undoing that needs no judgment call once policy is defined, so it
        # runs here rather than leaving it to the LLM to notice and decide.
        try:
            quality_guard_note = await run_quality_guard(settings, time.time())
        except Exception:
            logger.exception("quality guard check failed (non-fatal)")

        # Auto-restart a down media container (deterministic, cooldown-guarded).
        try:
            recovery_note = await _deterministic_service_recovery(mcp_client, time.time())
        except Exception:
            logger.exception("service recovery check failed (non-fatal)")

    # Spelled out as an explicit checklist in the TASK message, not just
    # buried in the system prompt — live testing found the model skip
    # nasdoom_queue entirely in a cycle where it was the one tool with an
    # actual finding, most likely because a run of hallucinated-kwarg
    # retries on other tools ate the round budget first. A direct per-turn
    # checklist is a stronger nudge than a system-prompt line the model has
    # to recall unprompted. Shared between the acting (scheduled) and
    # read_only (on-demand) paths so the checklist itself never drifts
    # between the two — only the preamble describing what already happened
    # (or didn't) differs.
    task = _READ_ONLY_PREAMBLE if read_only else _ACTING_PREAMBLE
    text, _history, _pending_action, _known_entity_ids = await run_agent_turn(
        task + "\n\n" + FINDINGS_INSTRUCTION,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_monitor_model,
        allowed_tools=MONITOR_READ_ONLY_TOOLS
        if read_only
        else MONITOR_READ_ONLY_TOOLS | MONITOR_ACTION_TOOLS | MONITOR_ESCALATION_TOOLS,
        action_tools=set() if read_only else MONITOR_ACTION_TOOLS,
        escalation_tools=set() if read_only else MONITOR_ESCALATION_TOOLS,
        ollama_url=settings.ollama_url,
        # Six independent signals to check in one turn, each a real chance
        # to consume a round on a hallucinated-then-retried kwarg (an
        # observed live pattern) — the default budget is tuned for a
        # 1-2-tool interactive reply, not a full sweep. Bumped from an
        # initial 14 after a live run still burned through it on kwarg
        # retries before reaching every check.
        max_tool_rounds=20,
    )
    # Deterministic-fix notes always surface (they describe real actions
    # taken or real import problems flagged), even on an otherwise-silent
    # cycle where the LLM returned the no-alert sentinel.
    llm_findings = [] if text.strip() == NO_ALERT_SENTINEL else parse_findings(text)
    return _notes_to_findings(queue_fix_note, strike_note, quality_guard_note, recovery_note) + llm_findings


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

    findings = asyncio.run(run_monitor_cycle(args.model))
    fp = fingerprint(findings)
    if fp is None and not args.force_alert_test:
        print(f"[{NO_ALERT_SENTINEL}] nothing actionable this cycle")
        # Nothing active — clear the dedup state so a later recurrence of the
        # same issue alerts fresh instead of being suppressed as a duplicate.
        _save_alert_state({})
        return

    # Alert-once-then-quiet: don't re-push an identical unresolved alert every
    # cycle. --force-alert-test bypasses the dedup (it's a manual test).
    should_push, new_state = _alert_decision(fp, _load_alert_state(), time.time())
    run_id = save_run(findings)
    text, markup = render_triage(findings, run_id=run_id)
    # Keep the rendered report TEXT alongside the dedup hash: the interactive
    # bot injects the active alert into its context, so when the operator
    # replies "investigate and fix it" the bot knows what "it" is (the alert
    # was pushed by THIS process — it's not in the bot's own chat history).
    # Cleared with the rest of the state on the all-clear path.
    new_state["text"] = text
    _save_alert_state(new_state)
    print(text)

    if not should_push and not args.force_alert_test:
        print("[suppressed] same alert already pushed; staying quiet until it changes or the remind interval")
        return

    if not args.no_telegram:
        settings = MonitorSettings()  # type: ignore[call-arg]
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message_html(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    text,
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
