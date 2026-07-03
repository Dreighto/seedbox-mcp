from __future__ import annotations

from seedbox_mcp.graduation import analyze_readiness, build_nudge

ACTION = {"restart", "fix_import", "grab"}
MONITOR = {"queue_cmd"}

NOW = 1_000_000_000.0
DAY = 86400.0


def _row(tool: str, outcome: str, dry: bool = False, target: str = "x", age_days: float = 1.0) -> dict:
    return {
        "tool": tool,
        "outcome": outcome,
        "dry_run": dry,
        "args": {"name": target, "confirm": True},
        "ts": NOW - age_days * DAY,
    }


def _by(rows):
    return {x.tool: x for x in analyze_readiness(rows, ACTION, MONITOR, NOW, threshold=5, min_distinct=2)}


def test_autonomous_regardless_of_count() -> None:
    r = _by([_row("queue_cmd", "ok") for _ in range(12)])
    assert r["queue_cmd"].verdict == "autonomous"


def test_ready_needs_threshold_AND_distinct_scenarios() -> None:
    # 5 clean successes across 3 distinct targets -> ready
    rows = [_row("grab", "ok", target=t) for t in ("a", "b", "c", "d", "e")]
    assert _by(rows)["grab"].verdict == "ready"


def test_five_successes_same_target_is_not_ready() -> None:
    # 5 clean but all the SAME target -> only 1 distinct scenario -> proving
    rows = [_row("grab", "ok", target="same") for _ in range(5)]
    x = _by(rows)["grab"]
    assert x.verdict == "proving"
    assert x.recent_success == 5 and x.distinct_scenarios == 1


def test_stale_success_expires_out_of_window() -> None:
    # 5 distinct successes but all 40 days ago (window is 30) -> unproven
    rows = [_row("grab", "ok", target=t, age_days=40) for t in ("a", "b", "c", "d", "e")]
    x = _by(rows)["grab"]
    assert x.recent_success == 0
    assert x.verdict == "unproven"
    assert x.real_success == 5  # all-time still counts them


def test_failed_outcome_forces_review() -> None:
    rows = [_row("restart", "ok", target=t) for t in ("a", "b", "c", "d", "e")]
    rows.append(_row("restart", "failed: not_permitted", target="f"))
    assert _by(rows)["restart"].verdict == "review"


def test_error_outcome_also_counts_as_fail() -> None:
    rows = [_row("restart", "ok", target=t) for t in ("a", "b")]
    rows.append(_row("restart", "error: boom", target="c"))
    assert _by(rows)["restart"].verdict == "review"


def test_unproven_when_no_real_runs() -> None:
    assert _by([_row("fix_import", "blocked_no_pending_match", dry=True)])["fix_import"].verdict == "unproven"


def test_nudge_fires_on_ready() -> None:
    rows = [_row("grab", "ok", target=t) for t in ("a", "b", "c", "d", "e")]
    nudge = build_nudge(analyze_readiness(rows, ACTION, MONITOR, NOW, threshold=5, min_distinct=2))
    assert nudge is not None and "grab has proven itself" in nudge


def test_nudge_silent_when_only_proving() -> None:
    rows = [_row("grab", "ok", target="same") for _ in range(5)]
    assert build_nudge(analyze_readiness(rows, ACTION, MONITOR, NOW, threshold=5, min_distinct=2)) is None
