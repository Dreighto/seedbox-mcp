from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from fastmcp import Client

from whatbox_media_mcp.chat.config import ChatSettings

logger = logging.getLogger("whatbox_chat.ai")


DEFAULT_SYSTEM_PROMPT = """\
You are a friendly media assistant for a personal Plex server. \
You help the user find out what's in the library, what's downloading, \
and manage their media collection. You have access to tools for Plex, Radarr, and Sonarr.

The `media_search` tool is the first starting point for many queries, to fuzzy search on title, \
or on an attribute like genre. Use `include_external_lookup` and `include_existing` to include \
results from the general TMDB database or just the existing seedbox services respectively. For \
example, set `include_external_lookup` to False if asked if we have a film, or if finding an \
internal ID to delete something. Narrow on `types` to reduce noise where possible. This tool \
returns all available internal IDs you'll need for other tools.

Unless specified otherwise, delete requests should delete the file and not blocklist the release.

`tautulli_history_tool` provides recent user activity.

`plex_library_size_tool` shows the library size. When it gets past 3.35TB we might be at the limit \
and this could cause stuck queues.

Rules:
- Be warm, concise, and plain-spoken. Avoid technical jargon unless asked. Do not mention \
any media ID from any called service or internal filepaths.
- For any action that adds or removes media, always call the tool with confirm=False first \
to get a preview. Present the preview clearly and ask the user to confirm before proceeding.
- Only call the tool again with confirm=True after the user explicitly says yes.
- If the user declines, acknowledge and do nothing further.
- You do not have access to the internet. If asked about things outside the media library, \
politely say you can only help with media on this server.\
"""


def load_system_prompt(settings: ChatSettings) -> str:
    if settings.system_prompt_path:
        return settings.system_prompt_path.read_text()
    return DEFAULT_SYSTEM_PROMPT


def mcp_tool_to_anthropic(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_tool_text(result: Any) -> str:
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


def _serialize_content(content: Any) -> Any:
    if isinstance(content, list):
        return [_serialize_content(c) for c in content]
    if hasattr(content, "model_dump"):
        return content.model_dump()
    return content


async def chat_turn(
    message: str,
    history: list[dict[str, Any]],
    settings: ChatSettings,
    mcp_client: Client[Any],
    anthropic_client: AsyncAnthropic,
) -> tuple[str, list[dict[str, Any]]]:
    async with mcp_client:
        raw_tools = await mcp_client.list_tools()

    tools = [mcp_tool_to_anthropic(t) for t in raw_tools]
    messages: list[dict[str, Any]] = list(history) + [{"role": "user", "content": message}]

    system_prompt = load_system_prompt(settings)

    while True:
        response = await anthropic_client.messages.create(
            model=settings.ai_model,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )

        if response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            for block in tool_uses:
                async with mcp_client:
                    result = await mcp_client.call_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": extract_tool_text(result),
                    }
                )
            messages.append({"role": "assistant", "content": _serialize_content(response.content)})
            messages.append({"role": "user", "content": tool_results})
        else:
            text = "\n".join(b.text for b in response.content if hasattr(b, "text"))
            messages.append({"role": "assistant", "content": _serialize_content(response.content)})
            return text, messages
