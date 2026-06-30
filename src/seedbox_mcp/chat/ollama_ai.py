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
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_s: float = 120.0,
) -> str:
    """Runs `task` through `model` (an Ollama-served model, typically a
    `:cloud`-tagged one) with tool-calling against every tool the connected
    MCP server exposes. Returns the final assistant text.

    One-shot, not a chat session — built for the routine-digest and bot-reply
    use cases, not an interactive multi-turn UI. Caps at MAX_TOOL_ROUNDS tool
    round-trips so a confused model can't loop forever."""
    async with mcp_client:
        raw_tools = await mcp_client.list_tools()
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
