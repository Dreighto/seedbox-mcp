from __future__ import annotations

from seedbox_mcp.quality_guard import HARD_BLOCK_SCORE, REPEAT_OFFENDER_THRESHOLD, evaluate_import


def test_clean_import_passes() -> None:
    assert evaluate_import("Bluray-1080p", [{"id": 39, "name": "DD+"}], 430) is None


def test_disc_image_tier_is_always_blocked_regardless_of_score() -> None:
    # The Frankenstein case: landed as BR-DISK with a matched custom format
    # score that isn't even negative — the quality-tier check must catch
    # this independently of the custom-format score.
    reason = evaluate_import("BR-DISK", [], 0)
    assert reason is not None
    assert "BR-DISK" in reason


def test_remux_blocked_via_hard_negative_custom_format_score() -> None:
    # The Super Mario Bros. case: parsed as Bluray-1080p at grab time (a
    # normal, allowed tier), but the actual file's matched custom format
    # (the "Remux (block)" format) carries a hard-block score.
    reason = evaluate_import("Bluray-1080p", [{"id": 114, "name": "Remux (block)"}], -10000)
    assert reason is not None
    assert "Remux (block)" in reason


def test_score_exactly_at_threshold_blocks() -> None:
    assert evaluate_import("Bluray-1080p", [{"name": "x"}], HARD_BLOCK_SCORE) is not None


def test_score_just_above_threshold_is_fine() -> None:
    assert evaluate_import("Bluray-1080p", [{"name": "x"}], HARD_BLOCK_SCORE + 1) is None


def test_normal_negative_preference_score_is_not_a_violation() -> None:
    # Ordinary upgrade-preference scoring (e.g. a slightly disfavored codec)
    # must not trip the guard — only HARD_BLOCK_SCORE-and-below counts.
    assert evaluate_import("Bluray-1080p", [{"name": "x265 (HD)"}], -50) is None


def test_repeat_offender_threshold_is_low_enough_to_actually_fire() -> None:
    # Sanity guard against an accidental future edit that sets this so high
    # the exact-title-block escape hatch never triggers in practice.
    assert 1 <= REPEAT_OFFENDER_THRESHOLD <= 5
