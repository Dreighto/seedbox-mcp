from __future__ import annotations

import pytest

import seedbox_mcp.telegram_bot_friend as bot
from seedbox_mcp.telegram_bot_friend import ChatState, _PUNT_FALLBACK, _PUNT_RE


def test_punt_regex_catches_real_logged_punts() -> None:
    for punt in (
        "Let me check for some newer scary movies — give me a second!",
        "Let me search for a good recent horror movie. One sec!",
        "I'll go ahead and request it for you now — give me a second!",
        "Hang tight, I'll get back to you.",
    ):
        assert _PUNT_RE.search(punt), punt


def test_punt_regex_ignores_legit_followup_promises() -> None:
    # "I'll let you know when it's ready" is REAL (the notifier sends it) and
    # "let me know if..." is asking THEM — neither may trigger the guard.
    for legit in (
        "I've sent your request to the owner. I'll let you know once it's ready.",
        "Approved and downloading. I'll message you the moment it's ready to watch.",
        "Is this the one — *The Godfather* (1972)? Let me know if you'd like to add it!",
    ):
        assert not _PUNT_RE.search(legit), legit


@pytest.mark.asyncio
async def test_punt_forces_continuation_and_sends_real_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter([
        ("Let me check for scary movies — one sec!", [{"role": "assistant"}], None, {}),
        ("*Sinners* (2025) is on Plex now. Want it?", [{"role": "assistant"}], None, {}),
    ])
    calls: list[str] = []

    async def fake_turn(task, **kwargs):
        calls.append(task)
        return next(replies)

    sent: list[str] = []

    async def fake_send_reply(token, chat_id, reply):
        sent.append(reply)

    async def fake_send(token, chat_id, text):
        sent.append(text)

    class _NoopHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return None

    monkeypatch.setattr(bot, "run_agent_turn", fake_turn)
    monkeypatch.setattr(bot, "_send_reply", fake_send_reply)
    monkeypatch.setattr(bot, "send_message", fake_send)
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **k: _NoopHTTP())
    monkeypatch.setattr(bot, "Client", lambda *a, **k: object())

    settings = bot.FriendBotSettings()
    state = ChatState(history=[], pending_action=None, known_entity_ids={})
    await bot._handle_message(settings, "tok", 1, "find me a scary movie", state, "Alex")

    assert len(calls) == 2  # original turn + one forced continuation
    assert "system note" in calls[1]
    assert sent == ["*Sinners* (2025) is on Plex now. Want it?"]  # punt never sent


@pytest.mark.asyncio
async def test_stubborn_punt_gets_honest_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def always_punt(task, **kwargs):
        return ("Give me a second!", [{"role": "assistant"}], None, {})

    sent: list[str] = []

    async def fake_send_reply(token, chat_id, reply):
        sent.append(reply)

    class _NoopHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return None

    monkeypatch.setattr(bot, "run_agent_turn", always_punt)
    monkeypatch.setattr(bot, "_send_reply", fake_send_reply)
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **k: _NoopHTTP())
    monkeypatch.setattr(bot, "Client", lambda *a, **k: object())

    settings = bot.FriendBotSettings()
    state = ChatState(history=[], pending_action=None, known_entity_ids={})
    await bot._handle_message(settings, "tok", 1, "find me a movie", state, "Alex")

    # after 2 forced continuations it gives up HONESTLY — never ships the punt
    assert sent == [_PUNT_FALLBACK]
