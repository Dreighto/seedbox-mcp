from __future__ import annotations

from seedbox_mcp.graduation import analyze_readiness

ACTION = {"queue_cmd", "fix_import", "service_restart", "grab"}
MONITOR = {"queue_cmd"}


def _row(tool: str, outcome: str, dry: bool = False) -> dict:
    return {"tool": tool, "outcome": outcome, "dry_run": dry}


def test_autonomous_tool_is_classified_autonomous_regardless_of_count() -> None:
    rows = [_row("queue_cmd", "ok") for _ in range(12)]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR)}
    assert r["queue_cmd"].verdict == "autonomous"
    assert r["queue_cmd"].real_success == 12


def test_ready_when_threshold_met_zero_failures() -> None:
    rows = [_row("grab", "ok") for _ in range(5)]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR, threshold=5)}
    assert r["grab"].verdict == "ready"


def test_proving_when_below_threshold() -> None:
    rows = [_row("service_restart", "ok"), _row("service_restart", "ok")]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR, threshold=5)}
    assert r["service_restart"].verdict == "proving"
    assert r["service_restart"].real_success == 2


def test_unproven_when_no_real_runs() -> None:
    rows = [_row("fix_import", "blocked_no_pending_match", dry=True)]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR)}
    assert r["fix_import"].verdict == "unproven"
    assert r["fix_import"].real_success == 0
    assert r["fix_import"].blocked == 1


def test_any_real_failure_forces_review_not_ready() -> None:
    # 6 successes would be "ready", but one real error drops it to review.
    rows = [_row("grab", "ok") for _ in range(6)] + [_row("grab", "error: boom")]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR, threshold=5)}
    assert r["grab"].verdict == "review"
    assert r["grab"].real_fail == 1


def test_dry_runs_do_not_count_as_proof() -> None:
    rows = [_row("grab", "ok", dry=True) for _ in range(10)]
    r = {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR, threshold=5)}
    assert r["grab"].verdict == "unproven"
    assert r["grab"].dry_run == 10
    assert r["grab"].real_success == 0


def test_nudge_none_when_nothing_actionable() -> None:
    from seedbox_mcp.graduation import build_nudge

    rows = [_row("service_restart", "ok"), _row("service_restart", "ok")]  # proving, not ready
    assert build_nudge(analyze_readiness(rows, ACTION, MONITOR, threshold=5)) is None


def test_nudge_flags_ready_and_review() -> None:
    from seedbox_mcp.graduation import build_nudge

    rows = [_row("grab", "ok") for _ in range(5)]  # ready
    rows += [_row("fix_import", "ok"), _row("fix_import", "error: x")]  # review (has a failure)
    nudge = build_nudge(analyze_readiness(rows, ACTION, MONITOR, threshold=5))
    assert nudge is not None
    assert "grab has proven itself" in nudge
    assert "fix_import has 1 real failure" in nudge
