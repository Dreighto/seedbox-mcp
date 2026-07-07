from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import seedbox_mcp.telegram_bot_friend as bot
from seedbox_mcp.telegram_bot_friend import (
    ChatState,
    _BLAME_USER_FALLBACK,
    _BLAME_USER_RE,
    _DISPUTE_RE,
    _PRIOR_CLAIM_RE,
)


def test_dispute_regex_catches_real_pushback() -> None:
    for dispute in (
        "It's not there",
        "Not on Plex",
        "can't find it",
        "nothing shows up",
        "it's missing",
        "doesn't work",
    ):
        assert _DISPUTE_RE.search(dispute), dispute


def test_dispute_regex_ignores_unrelated_phrasing() -> None:
    for legit in (
        "when is it there?",
        "not sure I want to watch it",
    ):
        assert not _DISPUTE_RE.search(legit), legit


def test_prior_claim_regex_catches_availability_claims() -> None:
    for claim in (
        "It's already on Plex and ready to watch!",
        "Yes, that's available now",
        "Dune is in the library",
    ):
        assert _PRIOR_CLAIM_RE.search(claim), claim


def test_prior_claim_regex_ignores_non_claims() -> None:
    for legit in (
        "I can request it and you'll be watching in an hour",
        "you can watch trailers on YouTube",
    ):
        assert not _PRIOR_CLAIM_RE.search(legit), legit


def test_blame_user_regex_catches_blame_phrasing() -> None:
    for blame in (
        "might be a playback issue",
        "setting on your end",
        "your device",
        "message the owner directly to troubleshoot",
        "check your app",
    ):
        assert _BLAME_USER_RE.search(blame), blame


def test_blame_user_regex_ignores_non_blame_phrasing() -> None:
    for legit in (
        "I set it to download",
        "the owner already added it",
    ):
        assert not _BLAME_USER_RE.search(legit), legit


@pytest.mark.asyncio
async def test_blame_guard_forces_reverify_and_uses_clean_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot claimed something was on Plex, the user disputed it, and the
    first draft reply blamed the user's device/settings. The guard must force
    a re-verify continuation and ship the clean second reply, never the blame."""
    fake_turn = AsyncMock(
        side_effect=[
            (
                "It looks like it's marked partially available. If it's not showing up for you, "
                "it might be a playback issue or a setting on your end.",
                [{"role": "assistant"}],
                None,
                {},
            ),
            (
                "Sorry, I double-checked and the files aren't actually there. I can request it again for you.",
                [{"role": "assistant"}],
                None,
                {},
            ),
        ]
    )

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

    monkeypatch.setattr(bot, "run_agent_turn", fake_turn)
    monkeypatch.setattr(bot, "_send_reply", fake_send_reply)
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **k: _NoopHTTP())
    monkeypatch.setattr(bot, "Client", lambda *a, **k: object())

    settings = bot.FriendBotSettings()
    state = ChatState(
        history=[
            {
                "role": "assistant",
                "content": "That's Brooklyn Nine-Nine, it's already on Plex and ready to watch!",
            }
        ],
        pending_action=None,
        known_entity_ids={},
    )
    await bot._handle_message(settings, "tok", 1, "It's not there.", state, "Ivan")

    assert fake_turn.await_count == 2
    assert sent == [
        "Sorry, I double-checked and the files aren't actually there. I can request it again for you."
    ]
    assert sent[0] != _BLAME_USER_FALLBACK


@pytest.mark.asyncio
async def test_blame_guard_falls_back_honestly_if_blame_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def always_blames(task, **kwargs):
        return (
            "That might be a playback issue on your end, message the owner directly to troubleshoot.",
            [{"role": "assistant"}],
            None,
            {},
        )

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

    monkeypatch.setattr(bot, "run_agent_turn", always_blames)
    monkeypatch.setattr(bot, "_send_reply", fake_send_reply)
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **k: _NoopHTTP())
    monkeypatch.setattr(bot, "Client", lambda *a, **k: object())

    settings = bot.FriendBotSettings()
    state = ChatState(
        history=[{"role": "assistant", "content": "Yes, it's already on Plex and ready to watch!"}],
        pending_action=None,
        known_entity_ids={},
    )
    await bot._handle_message(settings, "tok", 1, "It's not there.", state, "Ivan")

    assert sent == [_BLAME_USER_FALLBACK]
