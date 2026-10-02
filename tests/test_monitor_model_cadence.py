import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from seedbox_mcp import monitor
from seedbox_mcp.config import Settings
from seedbox_mcp.triage import Finding, fingerprint

_CHECKS = (
    "_deterministic_queue_resume",
    "run_download_strike_check",
    "run_quality_guard",
    "_deterministic_service_recovery",
    "_deterministic_storage_check",
    "_deterministic_jellyseerr_scan_check",
)
_START = 100_000.0


@pytest.fixture
def cycle(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    configured = monitor.MonitorSettings(_env_file=None, **settings.model_dump())
    monkeypatch.setattr(monitor, "MonitorSettings", lambda: configured)
    monkeypatch.setattr(monitor, "MONITOR_MODEL_STATE_PATH", tmp_path / "model.json")
    monkeypatch.setattr(monitor.time, "time", lambda: _START)
    mocks = {}
    for check in (*_CHECKS, "_deterministic_model_liveness_check"):
        mocks[check] = AsyncMock(return_value=None)
        monkeypatch.setattr(monitor, check, mocks[check])
    mocks["run_agent_turn"] = AsyncMock(return_value=("NO_ALERT_NEEDED", [], None, {}))
    monkeypatch.setattr(monitor, "run_agent_turn", mocks["run_agent_turn"])
    return mocks


@pytest.mark.asyncio
async def test_every_half_hour_checks_run_but_models_run_every_two_hours(cycle, monkeypatch) -> None:
    for elapsed in (0, 1800, 3600, 5400, 7200):
        monkeypatch.setattr(monitor.time, "time", lambda elapsed=elapsed: _START + elapsed)
        assert await monitor.run_monitor_cycle() == []
    for check in _CHECKS:
        assert cycle[check].await_count == 5
    assert cycle["_deterministic_model_liveness_check"].await_count == 2
    assert cycle["run_agent_turn"].await_count == 2
    assert monitor._load_model_state()[0] == _START + 7200


@pytest.mark.asyncio
@pytest.mark.parametrize("check", _CHECKS)
async def test_unresolved_deterministic_finding_runs_models_early(cycle, monkeypatch, check) -> None:
    await monitor.run_monitor_cycle()
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    cycle[check].return_value = "Still unresolved, not auto-fixed; needs escalation."
    findings = await monitor.run_monitor_cycle()
    assert findings[0].auto_fixed is False
    assert cycle["run_agent_turn"].await_count == 2
    assert cycle["_deterministic_model_liveness_check"].await_count == 2
    assert "Still unresolved" in cycle["run_agent_turn"].call_args.args[0]


@pytest.mark.asyncio
async def test_successful_automatic_fix_does_not_trigger_model(cycle, monkeypatch) -> None:
    await monitor.run_monitor_cycle()
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    cycle["_deterministic_queue_resume"].return_value = "Queue was paused, resumed it automatically."
    findings = await monitor.run_monitor_cycle()
    assert findings[0].auto_fixed is True
    assert cycle["run_agent_turn"].await_count == 1


@pytest.mark.asyncio
async def test_model_findings_survive_skips_and_clear_on_next_model_run(cycle, monkeypatch) -> None:
    cycle["run_agent_turn"].return_value = (
        '[{"severity":"needs_fix","title":"Backup failed","real":true,"reason":"confirmed twice"}]',
        [],
        None,
        {},
    )
    first = await monitor.run_monitor_cycle()
    _, alert_state = monitor._alert_decision(fingerprint(first), {}, _START)
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    skipped = await monitor.run_monitor_cycle()
    assert skipped == first
    assert monitor._alert_decision(fingerprint(skipped), alert_state, _START + 1800) == (False, alert_state)
    assert monitor._alert_decision(fingerprint(skipped), alert_state, _START + monitor.REMIND_INTERVAL_S)[0]
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 7200)
    cycle["run_agent_turn"].return_value = ("NO_ALERT_NEEDED", [], None, {})
    assert await monitor.run_monitor_cycle() == []
    assert monitor._load_model_state()[1] == []


@pytest.mark.asyncio
async def test_deterministic_resolution_is_not_cached(cycle, monkeypatch) -> None:
    await monitor.run_monitor_cycle()
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    cycle["_deterministic_storage_check"].return_value = "Storage low, not auto-fixed; needs escalation."
    assert fingerprint(await monitor.run_monitor_cycle())
    cycle["_deterministic_storage_check"].return_value = None
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 3600)
    assert await monitor.run_monitor_cycle() == []


@pytest.mark.asyncio
async def test_read_only_bypasses_cadence_without_changing_scheduled_state(cycle, monkeypatch) -> None:
    await monitor.run_monitor_cycle()
    before = monitor.MONITOR_MODEL_STATE_PATH.read_text()
    for mock in cycle.values():
        mock.reset_mock()
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    await monitor.run_monitor_cycle(read_only=True)
    cycle["run_agent_turn"].assert_awaited_once()
    for check in (*_CHECKS, "_deterministic_model_liveness_check"):
        cycle[check].assert_not_awaited()
    assert monitor.MONITOR_MODEL_STATE_PATH.read_text() == before


@pytest.mark.asyncio
async def test_slow_model_turn_keeps_start_time_cadence(cycle, monkeypatch) -> None:
    async def slow_turn(*args, **kwargs):
        monkeypatch.setattr(monitor.time, "time", lambda: _START + 300)
        return "NO_ALERT_NEEDED", [], None, {}

    cycle["run_agent_turn"].side_effect = slow_turn
    await monitor.run_monitor_cycle()
    assert monitor._load_model_state()[0] == _START
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 7200)
    await monitor.run_monitor_cycle()
    assert cycle["run_agent_turn"].await_count == 2


@pytest.mark.asyncio
async def test_failed_model_attempt_keeps_findings_and_deterministic_reporting(cycle, monkeypatch) -> None:
    old = Finding("backup", "needs_fix", "Backup failed", True, "confirmed")
    monitor._save_model_state(_START - 7200, [old])
    cycle["_deterministic_queue_resume"].return_value = "Queue resumed it automatically."
    cycle["run_agent_turn"].side_effect = RuntimeError("upstream unavailable")
    findings = await monitor.run_monitor_cycle()
    assert findings[-1] == old
    assert findings[0].auto_fixed
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    assert (await monitor.run_monitor_cycle())[-1] == old
    assert cycle["run_agent_turn"].await_count == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"last_run_ts":Infinity,"findings":[]}',
        '{"last_run_ts":NaN,"findings":[]}',
        '{"last_run_ts":200000,"findings":[]}',
        '{"last_run_ts":1,"findings":[{}]}',
    ],
)
@pytest.mark.asyncio
async def test_invalid_state_runs_models_instead_of_silently_skipping(cycle, raw) -> None:
    monitor.MONITOR_MODEL_STATE_PATH.write_text(raw)
    await monitor.run_monitor_cycle()
    cycle["run_agent_turn"].assert_awaited_once()


def test_main_preserves_alert_state_during_a_skipped_model_cycle(cycle, monkeypatch, tmp_path) -> None:
    old = Finding("backup", "needs_fix", "Backup failed", True, "confirmed")
    monitor._save_model_state(_START - 1800, [old])
    monkeypatch.setattr(monitor, "ALERT_STATE_PATH", tmp_path / "alert.json")
    _, state = monitor._alert_decision(fingerprint([old]), {}, _START - 1800)
    monitor._save_alert_state(state)
    monkeypatch.setattr("sys.argv", ["monitor", "--no-telegram"])
    monkeypatch.setattr(monitor.escalations, "collect_finished", lambda _: [])
    monkeypatch.setattr(monitor.escalations, "load", lambda: {})
    monkeypatch.setattr(monitor, "save_run", lambda _: "test-run")
    monitor.main()
    after = json.loads(monitor.ALERT_STATE_PATH.read_text())
    assert after["hash"] == state["hash"]
    assert after["last_pushed_ts"] == state["last_pushed_ts"]
    cycle["run_agent_turn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_agent_preserves_new_liveness_failure_without_duplicates(cycle, monkeypatch) -> None:
    note = "Pro model failed HTTP 410; not auto-fixed, needs escalation."
    old_liveness = monitor._notes_to_findings(note)[0]
    backup = Finding("backup", "needs_fix", "Backup failed", True, "confirmed")
    monitor._save_model_state(_START - 7200, [backup, old_liveness])
    cycle["_deterministic_model_liveness_check"].return_value = note
    cycle["run_agent_turn"].side_effect = RuntimeError("upstream unavailable")
    findings = await monitor.run_monitor_cycle()
    assert findings[1:] == [backup, old_liveness]
    assert findings[0].title == "Monitor model check failed"
    assert monitor._load_model_state()[1] == findings
    monitor.MONITOR_MODEL_STATE_PATH.unlink()
    findings = await monitor.run_monitor_cycle()
    assert findings[1:] == [old_liveness]
    assert fingerprint(findings)
    assert monitor._load_model_state()[1] == findings


@pytest.mark.asyncio
async def test_offline_triage_eval_exercises_monitor_with_sandbox(cycle, monkeypatch) -> None:
    from evals.bot_eval import SandboxClient, check_triage_structure, run_triage_check

    real = AsyncMock()
    sandbox = SandboxClient(real)
    cycle["run_agent_turn"].return_value = (
        '[{"severity":"needs_fix","title":"Backup failed","real":true,"reason":"confirmed twice"}]',
        [],
        None,
        {},
    )
    # The eval helper replaces these globals; monkeypatch owns their restoration.
    monkeypatch.setattr(monitor, "Client", monitor.Client)
    monkeypatch.setattr(monitor, "record_action", monitor.record_action)
    findings = await run_triage_check(sandbox)
    assert check_triage_structure(findings) == {"total": 1, "actionable": 1}
    assert cycle["run_agent_turn"].call_args.kwargs["mcp_client"] is sandbox
    real.__aenter__.assert_not_awaited()
    real.call_tool.assert_not_awaited()
    skipped = await run_triage_check(sandbox)
    assert skipped == findings
    assert cycle["run_agent_turn"].await_count == 1
    for check in _CHECKS:
        assert cycle[check].await_count == 2


@pytest.mark.asyncio
async def test_first_failed_model_run_is_reported_and_next_success_clears_it(cycle, monkeypatch) -> None:
    cycle["run_agent_turn"].side_effect = RuntimeError("upstream unavailable")
    findings = await monitor.run_monitor_cycle()
    assert len(findings) == 1
    assert findings[0].title == "Monitor model check failed"
    assert fingerprint(findings)
    assert monitor._load_model_state()[1] == findings
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 1800)
    assert await monitor.run_monitor_cycle() == findings
    assert cycle["run_agent_turn"].await_count == 1
    cycle["run_agent_turn"].side_effect = None
    monkeypatch.setattr(monitor.time, "time", lambda: _START + 7200)
    assert await monitor.run_monitor_cycle() == []
    assert monitor._load_model_state()[1] == []


@pytest.mark.asyncio
async def test_liveness_exception_still_runs_model_and_read_only_does_not_ping(cycle, monkeypatch) -> None:
    cycle["_deterministic_model_liveness_check"].side_effect = RuntimeError("ping unavailable")
    assert await monitor.run_monitor_cycle() == []
    cycle["run_agent_turn"].assert_awaited_once()
    assert monitor._load_model_state()[0] == _START
    before = monitor.MONITOR_MODEL_STATE_PATH.read_text()
    assert await monitor.run_monitor_cycle(read_only=True) == []
    assert cycle["run_agent_turn"].await_count == 2
    cycle["_deterministic_model_liveness_check"].assert_awaited_once()
    assert monitor.MONITOR_MODEL_STATE_PATH.read_text() == before
