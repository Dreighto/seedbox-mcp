from __future__ import annotations

import importlib

import pytest


def test_friend_bot_tools_are_within_the_safe_allowlist() -> None:
    mod = importlib.import_module("seedbox_mcp.telegram_bot_friend")
    tools = mod.FRIEND_READ_ONLY_TOOLS | mod.FRIEND_ACTION_TOOLS
    # Every exposed tool must be on the explicit safe allowlist.
    assert tools <= mod._FRIEND_SAFE_ALLOWLIST
    # And none may be a known system-affecting tool.
    assert not (tools & mod._FRIEND_FORBIDDEN)


def test_guard_would_reject_a_system_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a careless edit that adds a host-control tool, and confirm
    the import-time guard logic refuses it rather than silently exposing it
    to outside users."""
    mod = importlib.import_module("seedbox_mcp.telegram_bot_friend")
    leaked = mod.FRIEND_READ_ONLY_TOOLS | {"nas_service_restart"}
    # The subset check is the real containment boundary.
    assert not (leaked <= mod._FRIEND_SAFE_ALLOWLIST)
    # And the denylist tripwire also catches this specific class.
    assert (leaked | mod.FRIEND_ACTION_TOOLS) & mod._FRIEND_FORBIDDEN


def test_no_system_or_portal_tool_can_be_on_the_allowlist() -> None:
    """The allowlist itself must never contain a system/portal/escalation
    tool, so no future edit can 'allow' one by adding it there."""
    mod = importlib.import_module("seedbox_mcp.telegram_bot_friend")
    forbidden_ever = {
        "nas_service_restart",
        "nasdoom_add",
        "nasdoom_queue_command",
        "nasdoom_share_friend_create",
        "escalate_to_worker",
        "nas_disk_health",
        "nasdoom_find_grab",
    }
    assert not (mod._FRIEND_SAFE_ALLOWLIST & forbidden_ever)
