from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from seedbox_mcp.action_audit import MAX_ACTIONS_PER_HOUR, rate_limit_exceeded, record_action

logger = logging.getLogger("seedbox_mcp.chat.ollama_ai")

# Ollama's native /api/chat tool-calling. Cloud-tagged models (":cloud" suffix)
# run on Ollama's datacenter, billed flat-rate under the Pro subscription —
# this harness exists specifically to spend that subscription instead of a
# metered API, so default to local daemon proxying to the cloud model rather
# than calling api.ollama.com directly (that endpoint 401s for /api/chat —
# cloud inference is reached through `ollama signin` + the local daemon, not
# a bearer token to the public API).
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Every call previously omitted keep_alive, so Ollama Cloud applied whatever
# its own default eviction window is — operator reported ~4-5 min cold-start
# latency on the first message after an idle gap, consistent with the cloud
# side unloading the model and needing to reload it. Explicitly requesting a
# longer residency on every call means a normal gap between messages
# shouldn't trigger a reload; this can only help (never worse than the
# implicit default) regardless of what that default actually was — Ollama
# doesn't expose cloud-side model residency for inspection the way local
# `/api/ps` does for on-box models, so there was nothing to directly verify
# the prior default against.
KEEP_ALIVE = "30m"
MAX_TOOL_ROUNDS = 6

# The NAS-ops harness's current capability boundary (digest + telegram_bot).
# Every READ_ONLY tool the MCP server registers (see server.py) — explicitly
# excludes every WRITE/DESTRUCTIVE one (radarr/sonarr add/delete/queue_action)
# even though list_tools() would otherwise hand them to the model. Extend
# this set deliberately when adding real write capability, not by accident.
READ_ONLY_TOOLS: set[str] = {
    "media_status",
    "radarr_overview",
    "sonarr_overview",
    "plex_overview",
    "plex_library_size",
    "media_search",
    "staleness_report",
    "tautulli_history",
    "tautulli_users",
    "plex_now_playing",
    "tautulli_user_stats",
    "nas_backup_health",
    "nas_storage_inventory",
    "nas_internet_speed_test",
    "prowlarr_overview",
    "sabnzbd_overview",
    "jellyseerr_overview",
    "nasdoom_health",
    "fleet_health",
    "nasdoom_queue",
    "nasdoom_omni_search",
    "nasdoom_requests_overview",
    "nasdoom_control",
    "nasdoom_match_search",
    "nasdoom_find",
    "nasdoom_share_friends_list",
    "nasdoom_share_files_list",
    "radarr_calendar",
    "sonarr_calendar",
    "radarr_blocklist",
    "sonarr_blocklist",
    "prowlarr_indexer_stats",
    "nasdoom_profiles",
    "web_search",
    "web_fetch",
    "content_release_status",
    "poster_ocr",
    "jellyseerr_search",
    "nasdoom_releases",
    "nas_disk_health",
    "nas_service_status",
    "nas_import_diagnosis",
    "nas_log_search",
}

# Actions the harness may take, gated by the confirm=false|true preview
# pattern (see action_audit.py) rather than free rein — every real
# (confirm=true) call here is audit-logged and counts against
# MAX_ACTIONS_PER_HOUR. Started as "Tier 1: reversible only" (pause/resume,
# approve/decline, re-match); nasdoom_find_grab acquires real content
# (spends bandwidth/disk, not reversible in the undo sense) but got folded
# in once the preview/audit/rate-limit machinery existed to gate it properly
# — the safety mechanism, not the tool's reversibility, is what earns a spot
# here now. nasdoom_add (2026-07-01, operator request) adds real,
# non-trivial library content — deliberate scope increase for THIS bot
# specifically (operator-only, hard chat_id allowlist): the plan for the
# eventual friend-facing bot is to start from this same tool surface and
# remove capability, not build a separate one from scratch, so
# add/research/queue-action/blocklist-remove land here now even though a
# friend-facing variant would likely drop several of them. nasdoom_add is
# used INSTEAD OF the raw radarr_add_movie/sonarr_add_series (still
# registered on the MCP server, deliberately not in this set) — NASDOOM's
# endpoint routes anime vs regular content to the correct root
# folder/profile automatically; the raw tools take a single hardcoded
# default regardless of content type and would mis-route anime into the
# wrong folder (a real bug NASDOOM's own implementation already found and
# fixed — see tools/nasdoom.py's nasdoom_add docstring). Don't re-add the
# raw tools here without solving that routing problem first.
# radarr_delete_movie/sonarr_delete_series (DESTRUCTIVE, irreversible) and
# general storage cleanup are still deliberately NOT here — that bar is
# higher than this tier's, not built yet.
ACTION_TOOLS: set[str] = {
    "nasdoom_queue_command",
    "nasdoom_queue_item_command",
    "nasdoom_requests_action",
    "nasdoom_match_apply",
    "nasdoom_find_grab",
    "nasdoom_share_friend_create",
    "nasdoom_share_friend_revoke",
    "nasdoom_add",
    "radarr_research_movie",
    "sonarr_research_series",
    "sonarr_monitor_season",
    "radarr_queue_action",
    "sonarr_queue_action",
    "radarr_blocklist_remove",
    "sonarr_blocklist_remove",
    "jellyseerr_request_add",
    "nasdoom_grab_release",
    "nasdoom_fix_import",
    # nas_service_restart is a real service interruption but bounded (the
    # tool itself enforces a media-stack allowlist and verifies the
    # post-restart state in code) — same "safety mechanism earns the spot"
    # logic as nasdoom_find_grab above.
    "nas_service_restart",
}

# Escalation — not itself an action against the NAS, just the "call for
# backup" tool. Kept separate from ACTION_TOOLS so a caller can compose
# READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS deliberately rather than
# getting it bundled into either tier by accident.
ESCALATION_TOOLS: set[str] = {"escalate_to_worker"}

# Tools whose confirm=true must come in a LATER turn than their preview —
# the model may not self-approve within one turn. Adversarial testing
# (2026-07-01) caught the bot previewing a service restart, seeing the
# service was actually RUNNING (contradicting the operator's "seems down"
# premise), and firing confirm=true anyway "for good measure", all in one
# turn. For an action that interrupts a live service, the preview exists
# so the OPERATOR sees the current state before it runs — enforced here
# by timestamp (preview created during the current run_agent_turn call →
# confirm rejected), not by trusting the model to pause.
CROSS_TURN_CONFIRM_TOOLS: set[str] = {"nas_service_restart"}

# Entity-ID provenance gate — generalizes the same "don't trust the model's
# own regeneration, verify against real state" principle behind
# pending_action, applied to a different failure mode: instead of a
# fabricated ACTION, this catches a fabricated ARGUMENT — an id-shaped value
# (tmdb_id, tvdb_id, ...) the model invented or recalled from training data
# instead of getting it from an actual tool result in this conversation. A
# docstring saying "must come from media_search, never recall or construct
# one" is a request, not an enforcement — this is the enforcement. Extend
# both dicts together whenever a new tool takes an entity id that should
# always trace back to a real lookup: add the tool+param here, and make sure
# whatever tool actually returns that id uses a matching key name (or add
# the alternate spelling to ENTITY_ID_RESULT_KEYS).
ENTITY_ID_PARAMS: dict[str, set[str]] = {
    "nasdoom_add": {"tmdb_id", "tvdb_id"},
    "jellyseerr_request_add": {"tmdb_id"},
    "nasdoom_releases": {"tmdb_id"},
    "nasdoom_fix_import": {"tmdb_id"},
}
ENTITY_ID_RESULT_KEYS: dict[str, set[str]] = {
    # "tmdb"/"tvdb" (bare) cover nasdoom_omni_search's nested {"ids": {"tmdb":
    # ..., "tvdb": ...}} shape — found live: without these, a correct
    # search-then-add flow through omni_search got permanently blocked by
    # this same gate, since its ids never matched the tmdb_id/tmdbId-only
    # alias list. Verify any NEW search tool's actual key shape live before
    # assuming it matches — don't guess from the tool's own docstring.
    "tmdb_id": {"tmdb_id", "tmdbId", "tmdb"},
    "tvdb_id": {"tvdb_id", "tvdbId", "tvdb"},
}


def _scan_for_entity_ids(obj: Any, known: dict[str, list[int]]) -> None:
    """Recursively walk a parsed tool-result JSON structure, recording any
    value found under a key in ENTITY_ID_RESULT_KEYS into `known` (mutated
    in place). Runs after every successful tool call, not just ones on the
    ENTITY_ID_PARAMS tools — an id can legitimately surface from a read-only
    lookup several turns before it's used in a write."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            for canonical, aliases in ENTITY_ID_RESULT_KEYS.items():
                if key in aliases and isinstance(value, int) and not isinstance(value, bool):
                    bucket = known.setdefault(canonical, [])
                    if value not in bucket:
                        bucket.append(value)
            _scan_for_entity_ids(value, known)
    elif isinstance(obj, list):
        for item in obj:
            _scan_for_entity_ids(item, known)


def mcp_tool_to_ollama(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def extract_tool_text(result: Any) -> str:
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


# Alternate key names observed for the "tool name" field across inline-JSON
# shapes — models drift on this ("name" vs "method" vs "tool" vs "function").
_INLINE_NAME_KEYS = ("name", "method", "tool", "function")


def _parse_inline_tool_call(content: str) -> dict[str, Any] | None:
    """Some Ollama models (notably smaller/locally-tuned ones) emit a tool
    call as inline JSON in message content instead of structured tool_calls.
    Mirrors the same fallback the companion voice path already needed for
    companion-v1-voice — keep this even though the cloud models tested so far
    return proper structured tool_calls, since a model swap could reintroduce
    the quirk silently.

    Handles both the flat {"name": ..., "arguments": ...} shape and a nested
    {"content": {"method": ..., "arguments": ...}} shape observed live from
    gpt-oss:20b-cloud — a model that decided to describe a tool call as
    formatted JSON prose instead of either calling it for real or writing a
    plain-English answer. Anything JSON-shaped that looks like it was
    *trying* to be a tool call but doesn't match either shape gets logged
    (not silently dropped) so this failure mode is visible instead of only
    surfacing as a live "claimed it did something, did nothing" trap."""
    start = content.find("{")
    if start == -1:
        return None
    try:
        candidate = json.loads(content[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, dict):
        return None

    def _extract(d: dict[str, Any]) -> dict[str, Any] | None:
        name_key = next((k for k in _INLINE_NAME_KEYS if k in d), None)
        if name_key is not None and "arguments" in d:
            return {"name": d[name_key], "arguments": d["arguments"]}
        return None

    direct = _extract(candidate)
    if direct is not None:
        return direct

    nested = candidate.get("content")
    if isinstance(nested, dict):
        via_nested = _extract(nested)
        if via_nested is not None:
            return via_nested

    logger.warning(
        "ollama_ai: assistant content looked JSON-shaped but didn't match any known "
        "inline-tool-call pattern — treating as plain text: %.300s",
        content,
    )
    return None


# How long a previewed (confirm=false) action stays eligible to be confirmed.
# Exists so a "yes" minutes/hours later in an unrelated part of the
# conversation can't reach back and execute a stale, possibly-no-longer-
# accurate proposal — the operator would reasonably expect a confirmation
# that far removed from its preview to require a fresh look first.
PENDING_ACTION_TTL_S = 600.0


def _args_without_confirm(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "confirm"}


DEFAULT_MAX_HISTORY_MESSAGES = 40


def trim_history(
    history: list[dict[str, Any]], max_messages: int = DEFAULT_MAX_HISTORY_MESSAGES
) -> list[dict[str, Any]]:
    """Keeps the most recent `max_messages` history entries, but never cuts
    between an assistant tool_calls message and the tool results that answer
    it — a mid-call trim leaves the model staring at an orphaned tool_calls
    entry with no result, which reliably confuses it."""
    if len(history) <= max_messages:
        return history
    cut = len(history) - max_messages
    while cut < len(history) and history[cut].get("role") == "tool":
        cut += 1
    return history[cut:]


async def run_agent_turn(
    task: str,
    *,
    system_prompt: str,
    mcp_client: Client[Any],
    model: str,
    allowed_tools: set[str] | None = None,
    action_tools: set[str] | None = None,
    escalation_tools: set[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    pending_action: dict[str, Any] | None = None,
    known_entity_ids: dict[str, list[int]] | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 120.0,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, list[int]]]:
    """Runs `task` through `model` (an Ollama-served model, typically a
    `:cloud`-tagged one) with tool-calling against tools the connected MCP
    server exposes. Returns (final assistant text, updated history, updated
    pending_action, updated known_entity_ids).

    `history`: prior conversation turns (user/assistant/tool — NOT the system
    message, that's added fresh each call so a prompt change takes effect
    immediately). Pass the returned history back in on the next call for the
    same conversation; omit/None for a one-shot task with no continuity
    (what digest.py wants — a scheduled report has no "previous turn"). The
    caller owns persistence and trimming (trim_history helps) — this
    function only appends.

    `allowed_tools`: hard allowlist by tool name. The MCP server's
    READ_ONLY/WRITE/DESTRUCTIVE annotations (see server.py) are advisory
    metadata, not access control — `list_tools()` returns everything
    regardless, so relying on the system prompt alone ("you have no write
    tools") is a soft gate a confused model could ignore. This filters the
    schema handed to the model AND refuses to execute anything outside the
    set even if the model asks for it anyway (defense in depth against the
    inline-JSON tool-call fallback inventing a name). None = no restriction,
    for callers that intend full access (there are currently none — every
    caller passes an explicit set).

    `action_tools`/`escalation_tools`: which allowed tools get audit-logged
    and rate-limited. Every ACTION_TOOLS call with `confirm=true` in its args
    (the real thing, not a dry-run preview — see tools/nasdoom.py) and every
    ESCALATION_TOOLS call (no dry-run concept, dispatching a worker always
    does something) counts against MAX_ACTIONS_PER_HOUR — a circuit breaker
    independent of the model's own judgment, so a confused model looping on
    a real write can't just talk its way past a prompt instruction. See
    action_audit.py.

    `pending_action`: the single {tool, args, ts} the model is currently
    allowed to confirm — set by the most recent successful confirm=false
    preview of an ACTION_TOOLS call, cleared once confirmed (or superseded by
    a newer preview). This is the actual authorization gate for confirm=true:
    a confirm=true call only executes if its (tool, args-minus-confirm)
    matches what's pending and the preview isn't older than
    PENDING_ACTION_TTL_S. This exists because the model regenerating tool
    calls from free-form conversation history is not itself an authorization
    mechanism — live testing found a bare "yes" fabricating a pending action
    that was never proposed, and a pure meta-question ("what did you just
    do?") firing a real confirm=true write and then narrating it as a
    deliberate user-approved sequence. Server-tracked state, not the model's
    own judgment, is what makes confirm=true mean something. Persist the
    returned value the same way `history` is persisted — it's per-conversation
    state, not global.

    `known_entity_ids`: {param_name: [values seen in real tool results this
    conversation]} — see ENTITY_ID_PARAMS/ENTITY_ID_RESULT_KEYS. Updated
    automatically after every successful tool call (scans the result for
    tmdb_id/tvdb_id/etc.); enforced before executing any ACTION_TOOLS call
    listed in ENTITY_ID_PARAMS — an id-shaped argument that was never
    actually observed gets rejected rather than trusted. Same persistence
    contract as `pending_action`.

    `max_tool_rounds`: caps tool round-trips per call so a confused model
    can't loop forever within one turn. Defaults to MAX_TOOL_ROUNDS; a caller
    that needs a single turn to check many independent tools (e.g. a
    multi-signal monitor cycle) should pass a higher value — each hallucinated
    kwarg that gets retried also consumes a round, so "number of tools to
    check" isn't the only thing eating the budget."""
    action_tools = action_tools if action_tools is not None else ACTION_TOOLS
    known_entity_ids = {k: list(v) for k, v in (known_entity_ids or {}).items()}
    escalation_tools = escalation_tools if escalation_tools is not None else ESCALATION_TOOLS
    turn_started_ts = time.time()
    async with mcp_client:
        raw_tools = await mcp_client.list_tools()
    if allowed_tools is not None:
        raw_tools = [t for t in raw_tools if t.name in allowed_tools]
    tools = [mcp_tool_to_ollama(t) for t in raw_tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": task},
    ]

    async with httpx.AsyncClient(base_url=ollama_url, timeout=timeout_s) as http:
        for _round in range(max_tool_rounds):
            resp = await http.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            message = body.get("message", {})
            content = message.get("content", "") or ""

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                inline = _parse_inline_tool_call(content)
                if inline:
                    tool_calls = [{"function": inline}]

            if not tool_calls:
                messages.append({"role": "assistant", "content": content})
                # messages[0] is the system prompt, not part of persisted
                # history — everything from index 1 on (this turn's user
                # message onward) is what the next call should replay.
                return content, messages[1:], pending_action, known_entity_ids

            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                logger.info("ollama_ai tool call: %s(%s)", name, args)
                if allowed_tools is not None and name not in allowed_tools:
                    logger.error("ollama_ai BLOCKED disallowed tool call: %s(%s)", name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "content": '{"ok": false, "error_type": "not_permitted", '
                            '"message": "This tool is not available in this context."}',
                        }
                    )
                    continue

                is_action_tool = name in action_tools
                wants_confirm = is_action_tool and bool(args.get("confirm"))

                # The pending-action gate: a confirm=true call only counts as
                # authorized if it matches the single preview the harness is
                # currently holding (same tool, same args minus confirm,
                # previewed within the TTL). Without this, "was this
                # authorized" was entirely the model's own free-form
                # reinterpretation of conversation history on every turn —
                # live testing found that let a bare "yes" fabricate an
                # action nothing proposed, and a pure meta-question fire a
                # real write the model then narrated as deliberately
                # user-approved. This is what actually makes confirm=true
                # mean something, not just a convention the model follows.
                if wants_confirm:
                    normalized = _args_without_confirm(args)
                    matches_pending = (
                        pending_action is not None
                        and pending_action.get("tool") == name
                        and pending_action.get("args") == normalized
                        and (time.time() - pending_action.get("ts", 0)) <= PENDING_ACTION_TTL_S
                    )
                    if not matches_pending:
                        logger.error(
                            "ollama_ai BLOCKED confirm=true with no matching pending preview: %s(%s)",
                            name,
                            args,
                        )
                        record_action(name, args, dry_run=True, outcome="blocked_no_pending_match")
                        messages.append(
                            {
                                "role": "tool",
                                "content": (
                                    '{"ok": false, "error_type": "not_permitted", "message": '
                                    '"No matching preview is currently pending for this exact action. '
                                    "Call with confirm=false first to preview it, then confirm=true "
                                    'right after — a confirm=true out of nowhere is not authorized."}'
                                ),
                            }
                        )
                        continue
                    if (
                        name in CROSS_TURN_CONFIRM_TOOLS
                        and pending_action is not None
                        and pending_action.get("ts", 0) >= turn_started_ts
                    ):
                        logger.error(
                            "ollama_ai BLOCKED same-turn confirm for cross-turn tool: %s(%s)", name, args
                        )
                        record_action(name, args, dry_run=True, outcome="blocked_same_turn_confirm")
                        messages.append(
                            {
                                "role": "tool",
                                "content": (
                                    '{"ok": false, "error_type": "not_permitted", "message": '
                                    '"This action needs the operator\'s explicit go-ahead in their NEXT '
                                    "message. The preview ran; now STOP, show the operator the preview's "
                                    "current state, and wait — do not confirm in the same turn, and do "
                                    'not re-preview."}'
                                ),
                            }
                        )
                        continue
                    # Matched and about to execute for real — consume it so
                    # the same proposal can't be confirmed twice.
                    pending_action = None

                # A "real" action = an escalation (no dry-run concept, it
                # always dispatches a worker) or an ACTION_TOOLS call with
                # confirm=true that just cleared the pending-action gate
                # above (a dry-run preview doesn't write anything, so it
                # doesn't count against the breaker or get logged as one).
                # Entity-ID provenance gate: for tools in ENTITY_ID_PARAMS,
                # any listed argument that's set must be a value actually
                # observed in a real tool result this conversation — not a
                # plausible-looking id the model recalled from training data
                # or invented. Checked regardless of confirm=false/true (a
                # fabricated id in a *preview* is just as wrong as in a real
                # write — the operator would be confirming a lie).
                required_ids = ENTITY_ID_PARAMS.get(name)
                if required_ids:
                    unverified = [
                        (param, args[param])
                        for param in required_ids
                        if args.get(param) is not None and args[param] not in known_entity_ids.get(param, [])
                    ]
                    if unverified:
                        logger.error("ollama_ai BLOCKED unverified entity id(s): %s(%s) -> %s", name, args, unverified)
                        messages.append(
                            {
                                "role": "tool",
                                "content": (
                                    '{"ok": false, "error_type": "not_permitted", "message": '
                                    f'"{unverified[0][0]}={unverified[0][1]!r} was not found in this '
                                    'conversation\'s search/lookup results — never invent or recall an id '
                                    'from memory. Call media_search or nasdoom_omni_search first and use '
                                    'the id it returns."}}'
                                ),
                            }
                        )
                        continue

                is_escalation = name in escalation_tools
                is_real_action = is_escalation or wants_confirm

                if is_real_action and rate_limit_exceeded():
                    logger.error("ollama_ai RATE LIMITED: %s(%s)", name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "content": (
                                '{"ok": false, "error_type": "rate_limited", "message": '
                                f'"More than {MAX_ACTIONS_PER_HOUR} real actions in the last hour — '
                                'refusing to execute more until this cools down. Tell the operator '
                                'this happened, don\'t just retry."}}'
                            ),
                        }
                    )
                    continue

                try:
                    async with mcp_client:
                        result = await mcp_client.call_tool(name, args)
                    tool_text = extract_tool_text(result)
                    with contextlib.suppress(json.JSONDecodeError):
                        _scan_for_entity_ids(json.loads(tool_text), known_entity_ids)
                    if is_real_action:
                        record_action(name, args, dry_run=False, outcome="ok")
                    elif is_action_tool:
                        # A successful confirm=false preview — this becomes
                        # the one thing a subsequent confirm=true is allowed
                        # to execute. Overwrites any prior pending proposal;
                        # only one can be live at a time.
                        pending_action = {
                            "tool": name,
                            "args": _args_without_confirm(args),
                            "ts": time.time(),
                        }
                except ToolError as exc:
                    # A model-invented bad arg shouldn't kill the whole run —
                    # feed the error back so it can self-correct, same as a
                    # tool returning ToolResponse.failure would.
                    logger.warning("ollama_ai tool call failed: %s(%s): %s", name, args, exc)
                    tool_text = f'{{"ok": false, "error_type": "invalid_call", "message": {str(exc)!r}}}'
                    if is_real_action:
                        record_action(name, args, dry_run=False, outcome=f"error: {exc}")
                messages.append({"role": "tool", "content": tool_text})

        timeout_note = "(stopped after max tool rounds without a final answer — task may need narrowing)"
        messages.append({"role": "assistant", "content": timeout_note})
        return timeout_note, messages[1:], pending_action, known_entity_ids
