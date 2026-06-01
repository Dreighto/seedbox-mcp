from __future__ import annotations

import os
from typing import Any

import pytest

from whatbox_media_mcp.chat.ai import chat_turn
from whatbox_media_mcp.chat.config import ChatSettings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_TESTS"), reason="set LIVE_TESTS=1 to run"),
]


def _real_settings() -> ChatSettings:
    return ChatSettings()  # type: ignore[call-arg]


def _real_anthropic(settings: ChatSettings) -> Any:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


def _real_mcp(settings: ChatSettings) -> Any:
    from whatbox_media_mcp.chat.mcp_client import make_mcp_client

    return make_mcp_client(settings)


@pytest.mark.asyncio
async def test_no_tool_turn() -> None:
    settings = _real_settings()
    mcp_client = _real_mcp(settings)
    anthropic_client = _real_anthropic(settings)

    reply, history = await chat_turn("hello", [], settings, mcp_client, anthropic_client)

    assert reply.strip(), "Expected a non-empty reply"
    # No tool calls: history should be [user, assistant] only
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant"], f"Unexpected history roles: {roles}"


@pytest.mark.asyncio
async def test_read_tool_turn() -> None:
    settings = _real_settings()
    mcp_client = _real_mcp(settings)
    anthropic_client = _real_anthropic(settings)

    reply, history = await chat_turn("What's currently downloading?", [], settings, mcp_client, anthropic_client)

    assert reply.strip(), "Expected a non-empty reply"
    # At least one tool call means history has > 2 entries
    assert len(history) > 2, "Expected at least one tool call in history"


@pytest.mark.asyncio
async def test_dry_run_turn() -> None:
    settings = _real_settings()
    mcp_client = _real_mcp(settings)
    anthropic_client = _real_anthropic(settings)

    reply, history = await chat_turn(
        "Please add the movie Paddington (2014) to Radarr",
        [],
        settings,
        mcp_client,
        anthropic_client,
    )

    assert reply.strip(), "Expected a non-empty reply"

    # Find any tool result messages containing dry_run
    tool_result_messages = [
        m
        for m in history
        if m["role"] == "user"
        and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]
    dry_run_seen = any(
        '"dry_run": true' in str(b.get("content", ""))
        for m in tool_result_messages
        for b in m["content"]
        if isinstance(b, dict)
    )
    assert dry_run_seen, "Expected a dry_run=true tool result before any confirmation"

    # confirm=True should NOT have been called — no confirmed execution
    confirm_true_seen = any(
        '"confirm": true' in str(m) or "'confirm': True" in str(m) for m in history if m["role"] == "assistant"
    )
    assert not confirm_true_seen, "Haiku should not have called confirm=True without user approval"
