from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

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
}


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


async def run_agent_turn(
    task: str,
    *,
    system_prompt: str,
    mcp_client: Client[Any],
    model: str,
    allowed_tools: set[str] | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 120.0,
) -> str:
    """Runs `task` through `model` (an Ollama-served model, typically a
    `:cloud`-tagged one) with tool-calling against tools the connected MCP
    server exposes. Returns the final assistant text.

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

    One-shot, not a chat session — built for the routine-digest and bot-reply
    use cases, not an interactive multi-turn UI. Caps at MAX_TOOL_ROUNDS tool
    round-trips so a confused model can't loop forever."""
    async with mcp_client:
        raw_tools = await mcp_client.list_tools()
    if allowed_tools is not None:
        raw_tools = [t for t in raw_tools if t.name in allowed_tools]
    tools = [mcp_tool_to_ollama(t) for t in raw_tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
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
                return content

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
                try:
                    async with mcp_client:
                        result = await mcp_client.call_tool(name, args)
                    tool_text = extract_tool_text(result)
                except ToolError as exc:
                    # A model-invented bad arg shouldn't kill the whole run —
                    # feed the error back so it can self-correct, same as a
                    # tool returning ToolResponse.failure would.
                    logger.warning("ollama_ai tool call failed: %s(%s): %s", name, args, exc)
                    tool_text = f'{{"ok": false, "error_type": "invalid_call", "message": {str(exc)!r}}}'
                messages.append({"role": "tool", "content": tool_text})

        return "(stopped after max tool rounds without a final answer — task may need narrowing)"
