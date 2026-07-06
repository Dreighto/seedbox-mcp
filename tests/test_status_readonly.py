from unittest.mock import AsyncMock, patch

import pytest

from seedbox_mcp.monitor import (
    MONITOR_ACTION_TOOLS,
    MONITOR_ESCALATION_TOOLS,
    MONITOR_READ_ONLY_TOOLS,
    run_monitor_cycle,
)

_MOD = "seedbox_mcp.monitor"


@pytest.mark.asyncio
async def test_read_only_skips_deterministic_fixers_and_strips_action_tools() -> None:
    with (
        patch(f"{_MOD}._keep_interactive_model_warm", new=AsyncMock(return_value=None)),
        patch(f"{_MOD}._deterministic_queue_resume", new=AsyncMock(return_value="should not run")) as queue_mock,
        patch(f"{_MOD}.run_download_strike_check", new=AsyncMock(return_value="should not run")) as strike_mock,
        patch(f"{_MOD}._deterministic_service_recovery", new=AsyncMock(return_value="should not run")) as recovery_mock,
        patch(f"{_MOD}.run_agent_turn", new=AsyncMock(return_value=("[]", [], None, {}))) as agent_mock,
    ):
        await run_monitor_cycle(read_only=True)

    queue_mock.assert_not_called()
    strike_mock.assert_not_called()
    recovery_mock.assert_not_called()

    agent_mock.assert_awaited_once()
    _args, kwargs = agent_mock.call_args
    assert kwargs["allowed_tools"] == MONITOR_READ_ONLY_TOOLS
    assert kwargs["action_tools"] == set()
    assert kwargs["escalation_tools"] == set()


@pytest.mark.asyncio
async def test_default_scheduled_cycle_still_runs_deterministic_fixers_and_keeps_action_tools() -> None:
    with (
        patch(f"{_MOD}._keep_interactive_model_warm", new=AsyncMock(return_value=None)),
        patch(f"{_MOD}._deterministic_queue_resume", new=AsyncMock(return_value=None)) as queue_mock,
        patch(f"{_MOD}.run_download_strike_check", new=AsyncMock(return_value=None)) as strike_mock,
        patch(f"{_MOD}._deterministic_service_recovery", new=AsyncMock(return_value=None)) as recovery_mock,
        patch(f"{_MOD}.run_agent_turn", new=AsyncMock(return_value=("[]", [], None, {}))) as agent_mock,
    ):
        await run_monitor_cycle()

    queue_mock.assert_awaited_once()
    strike_mock.assert_awaited_once()
    recovery_mock.assert_awaited_once()

    agent_mock.assert_awaited_once()
    _args, kwargs = agent_mock.call_args
    assert kwargs["allowed_tools"] == MONITOR_READ_ONLY_TOOLS | MONITOR_ACTION_TOOLS | MONITOR_ESCALATION_TOOLS
    assert kwargs["action_tools"] == MONITOR_ACTION_TOOLS
    assert kwargs["action_tools"] != set()
    assert kwargs["escalation_tools"] == MONITOR_ESCALATION_TOOLS
    assert kwargs["escalation_tools"] != set()
