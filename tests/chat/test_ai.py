from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from whatbox_media_mcp.chat.ai import (
    DEFAULT_SYSTEM_PROMPT,
    chat_turn,
    extract_tool_text,
    load_system_prompt,
    mcp_tool_to_anthropic,
)
from whatbox_media_mcp.chat.config import ChatSettings

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_load_system_prompt_returns_default_when_path_none(chat_settings: ChatSettings) -> None:
    assert load_system_prompt(chat_settings) == DEFAULT_SYSTEM_PROMPT


def test_load_system_prompt_reads_file_when_path_set(chat_settings: ChatSettings, tmp_path: Path) -> None:
    p = tmp_path / "prompt.txt"
    p.write_text("custom prompt")
    chat_settings.model_copy()
    s = chat_settings.model_copy(update={"system_prompt_path": p})
    assert load_system_prompt(s) == "custom prompt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_mcp_tool_to_anthropic_formats_correctly() -> None:
    tool = MagicMock()
    tool.name = "media_search"
    tool.description = "Search media"
    tool.inputSchema = {"type": "object", "properties": {}}
    result = mcp_tool_to_anthropic(tool)
    assert result == {
        "name": "media_search",
        "description": "Search media",
        "input_schema": {"type": "object", "properties": {}},
    }


def test_extract_tool_text_joins_text_blocks() -> None:
    block1 = MagicMock()
    block1.text = '{"ok": true}'
    block2 = MagicMock()
    del block2.text  # no text attr
    result_mock = MagicMock()
    result_mock.content = [block1, block2]
    assert extract_tool_text(result_mock) == '{"ok": true}'


# ---------------------------------------------------------------------------
# chat_turn
# ---------------------------------------------------------------------------


def _make_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [block]
    return response


def _make_tool_use_response(tool_name: str, tool_id: str, tool_input: dict[str, Any]) -> MagicMock:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = tool_id
    tool_block.name = tool_name
    tool_block.input = tool_input
    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    return response


def _make_mcp_tool() -> MagicMock:
    tool = MagicMock()
    tool.name = "media_search"
    tool.description = "Search media"
    tool.inputSchema = {"type": "object"}
    return tool


@pytest.mark.asyncio
async def test_chat_turn_no_tool_calls(chat_settings: ChatSettings) -> None:
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = AsyncMock(return_value=_make_text_response("Hello!"))

    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[_make_mcp_tool()])

    reply, history = await chat_turn("hi", [], chat_settings, mock_mcp, mock_anthropic)

    assert reply == "Hello!"
    assert mock_anthropic.messages.create.call_count == 1
    mock_mcp.call_tool.assert_not_called()
    # history should contain user message + assistant message
    assert history[-1]["role"] == "assistant"
    assert history[-2]["role"] == "user"


@pytest.mark.asyncio
async def test_chat_turn_with_tool_call(chat_settings: ChatSettings) -> None:
    tool_result_block = MagicMock()
    tool_result_block.text = '{"ok": true, "data": {}}'
    tool_call_result = MagicMock()
    tool_call_result.content = [tool_result_block]

    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=[
            _make_tool_use_response("media_search", "tu_1", {"query": "heat"}),
            _make_text_response("Heat is in your library."),
        ]
    )

    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[_make_mcp_tool()])
    mock_mcp.call_tool = AsyncMock(return_value=tool_call_result)

    reply, history = await chat_turn("find Heat", [], chat_settings, mock_mcp, mock_anthropic)

    assert reply == "Heat is in your library."
    assert mock_anthropic.messages.create.call_count == 2
    mock_mcp.call_tool.assert_called_once_with("media_search", {"query": "heat"})


@pytest.mark.asyncio
async def test_chat_turn_preserves_history(chat_settings: ChatSettings) -> None:
    existing_history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]},
    ]
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = AsyncMock(return_value=_make_text_response("Fine thanks."))

    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[])

    reply, history = await chat_turn("how are you?", existing_history, chat_settings, mock_mcp, mock_anthropic)

    # History passed to Anthropic should include prior turns
    call_args = mock_anthropic.messages.create.call_args
    messages_sent = call_args.kwargs["messages"]
    assert messages_sent[0]["role"] == "user"
    assert messages_sent[0]["content"] == "hello"
    assert len(history) == len(existing_history) + 2  # + new user + new assistant
