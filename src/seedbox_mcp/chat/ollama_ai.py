from __future__ import annotations

import json
import logging
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
    "tautulli_user_stats",
    "nas_backup_health",
    "nas_storage_inventory",
    "prowlarr_overview",
    "sabnzbd_overview",
    "jellyseerr_overview",
    "nasdoom_health",
    "nasdoom_queue",
    "nasdoom_omni_search",
    "nasdoom_requests_overview",
    "nasdoom_control",
    "nasdoom_match_search",
}

# Tier 1 — reversible, low-stakes actions the harness may take directly, no
# extra confirmation gate beyond the model's own judgment. Everything here
# is trivially undoable (pause/resume, approve/decline, re-match) — nothing
# that deletes or acquires content. See project memory for the tiering
# rationale (Tier 2/3 — media add/delete, storage cleanup — deliberately
# NOT here yet; those need a real preview-then-confirm pattern this harness
# doesn't have).
ACTION_TOOLS: set[str] = {
    "nasdoom_queue_command",
    "nasdoom_queue_item_command",
    "nasdoom_requests_action",
    "nasdoom_match_apply",
}

# Escalation — not itself an action against the NAS, just the "call for
# backup" tool. Kept separate from ACTION_TOOLS so a caller can compose
# READ_ONLY_TOOLS | ACTION_TOOLS | ESCALATION_TOOLS deliberately rather than
# getting it bundled into either tier by accident.
ESCALATION_TOOLS: set[str] = {"escalate_to_worker"}


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


def _parse_inline_tool_call(content: str) -> dict[str, Any] | None:
    """Some Ollama models (notably smaller/locally-tuned ones) emit a tool
    call as inline JSON in message content instead of structured tool_calls.
    Mirrors the same fallback the companion voice path already needed for
    companion-v1-voice — keep this even though the cloud models tested so far
    return proper structured tool_calls, since a model swap could reintroduce
    the quirk silently."""
    start = content.find("{")
    if start == -1:
        return None
    try:
        candidate = json.loads(content[start:])
    except json.JSONDecodeError:
        return None
    if isinstance(candidate, dict) and "name" in candidate and "arguments" in candidate:
        return candidate
    return None


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
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 120.0,
) -> tuple[str, list[dict[str, Any]]]:
    """Runs `task` through `model` (an Ollama-served model, typically a
    `:cloud`-tagged one) with tool-calling against tools the connected MCP
    server exposes. Returns (final assistant text, updated history).

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

    Caps at MAX_TOOL_ROUNDS tool round-trips per call so a confused model
    can't loop forever within one turn."""
    action_tools = action_tools if action_tools is not None else ACTION_TOOLS
    escalation_tools = escalation_tools if escalation_tools is not None else ESCALATION_TOOLS
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
        for _round in range(MAX_TOOL_ROUNDS):
            resp = await http.post(
                "/api/chat",
                json={"model": model, "messages": messages, "tools": tools, "stream": False},
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
                return content, messages[1:]

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

                # A "real" action = an escalation (no dry-run concept, it
                # always dispatches a worker) or an ACTION_TOOLS call with
                # confirm=true (a dry-run preview doesn't write anything, so
                # it doesn't count against the breaker or get logged as one).
                is_escalation = name in escalation_tools
                is_confirmed_action = name in action_tools and bool(args.get("confirm"))
                is_real_action = is_escalation or is_confirmed_action

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
                    if is_real_action:
                        record_action(name, args, dry_run=False, outcome="ok")
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
        return timeout_note, messages[1:]
