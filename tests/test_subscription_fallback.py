from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from seedbox_mcp import model_health
from seedbox_mcp.chat import ollama_ai, subscription_models

OLLAMA = "http://ollama.test"
LIMIT = {"error": "you have reached your weekly usage limit, upgrade for higher limits"}


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeMcp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        schema = {"type": "object", "properties": {"confirm": {"type": "boolean"}}}
        return [
            SimpleNamespace(name="media_status", description="health", inputSchema=schema),
            SimpleNamespace(name=sorted(ollama_ai.ACTION_TOOLS)[0], description="write", inputSchema=schema),
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true, "data": {"plex": "up"}}')])


def scripted(answers: list[str], seen: list[str]) -> Any:
    async def runner(prompt: str, timeout_s: float) -> str:
        seen.append(prompt)
        return answers.pop(0)

    return runner


def test_parse_step_reads_fenced_json_and_rejects_prose() -> None:
    message = subscription_models.parse_step(
        '```json\n{"content": "", "tool_calls": [{"name": "media_status", "arguments": {}}]}\n```'
    )
    assert message == {"content": "", "tool_calls": [{"function": {"name": "media_status", "arguments": {}}}]}
    with pytest.raises(ValueError):
        subscription_models.parse_step("Everything looks fine.")
    with pytest.raises(ValueError):
        subscription_models.parse_step('{"content": "x", "tool_calls": [{"arguments": {}}]}')


def test_friend_chats_get_claude_only() -> None:
    assert subscription_models.NO_FILE_ACCESS == ("claude",)
    assert subscription_models.ALL_BACKENDS[0] == "claude"


@pytest.mark.asyncio
@respx.mock
async def test_ollama_limit_moves_the_turn_to_subscription_models(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(429, json=LIMIT))
    prompts: list[str] = []
    answers = [
        '{"content": "", "tool_calls": [{"name": "media_status", "arguments": {}}]}',
        '{"content": "Plex is up.", "tool_calls": []}',
    ]
    monkeypatch.setitem(subscription_models.RUNNERS, "claude", scripted(answers, prompts))
    mcp = FakeMcp()

    text, history, _, _ = await ollama_ai.run_agent_turn(
        "is plex up?",
        system_prompt="Be brief.",
        mcp_client=mcp,
        model="m:cloud",
        ollama_url=OLLAMA,
        allowed_tools={"media_status"},
        fallback_models=("claude",),
    )

    assert text == "Plex is up."
    assert mcp.calls == [("media_status", {})]
    assert respx.calls.call_count == 1
    assert '{"role": "tool", "content": "{\\"ok\\": true' in prompts[1] and "Be brief." in prompts[0]
    assert history[-1] == {"role": "assistant", "content": "Plex is up."}


@pytest.mark.asyncio
@respx.mock
async def test_gates_still_apply_to_subscription_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(429, json=LIMIT))
    action = sorted(ollama_ai.ACTION_TOOLS)[0]
    answers = [
        json.dumps({"content": "", "tool_calls": [{"name": action, "arguments": {"confirm": True}}]}),
        '{"content": "I could not do that.", "tool_calls": []}',
    ]
    monkeypatch.setitem(subscription_models.RUNNERS, "claude", scripted(answers, []))
    monkeypatch.setattr(ollama_ai, "record_action", lambda *a, **k: None)
    mcp = FakeMcp()

    text, _, _, _ = await ollama_ai.run_agent_turn(
        "do it",
        system_prompt="s",
        mcp_client=mcp,
        model="m:cloud",
        ollama_url=OLLAMA,
        allowed_tools={action},
        fallback_models=("claude",),
    )

    assert mcp.calls == []
    assert text == "I could not do that."


@pytest.mark.asyncio
@respx.mock
async def test_next_backend_answers_and_all_failing_raises_the_original_429(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(429, json=LIMIT))

    async def broken(prompt: str, timeout_s: float) -> str:
        raise RuntimeError("claude exited 1")

    monkeypatch.setitem(subscription_models.RUNNERS, "claude", broken)
    monkeypatch.setitem(subscription_models.RUNNERS, "codex", scripted(['{"content": "ok", "tool_calls": []}'], []))
    text, _, _, _ = await ollama_ai.run_agent_turn(
        "hi",
        system_prompt="s",
        mcp_client=FakeMcp(),
        model="m:cloud",
        ollama_url=OLLAMA,
        fallback_models=("claude", "codex"),
    )
    assert text == "ok"

    with pytest.raises(httpx.HTTPStatusError):
        await ollama_ai.run_agent_turn(
            "hi",
            system_prompt="s",
            mcp_client=FakeMcp(),
            model="m:cloud",
            ollama_url=OLLAMA,
            fallback_models=("claude",),
        )


@pytest.mark.asyncio
@respx.mock
async def test_no_fallback_keeps_the_old_failure() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(429, json=LIMIT))
    with pytest.raises(httpx.HTTPStatusError):
        await ollama_ai.run_agent_turn(
            "hi",
            system_prompt="s",
            mcp_client=FakeMcp(),
            model="m:cloud",
            ollama_url=OLLAMA,
            fallback_models=(),
        )


@pytest.mark.asyncio
@respx.mock
async def test_usage_limit_is_not_reported_as_a_dead_model() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(429, json=LIMIT))
    assert await model_health.check_model(OLLAMA, "m:cloud") is None
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(410, json={"error": "model retired"}))
    assert await model_health.check_model(OLLAMA, "m:cloud") == "HTTP 410: model retired"
