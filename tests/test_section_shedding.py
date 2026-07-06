from __future__ import annotations

from seedbox_mcp.telegram_bot import MAX_ACTIVE_SECTIONS, _prioritized_sections


def test_no_matched_no_protected_drops_oldest_prior() -> None:
    # prior is MRU-first: 'a' most recent, 'd' oldest. Cap 3 keeps the
    # 3 most-recently-used and drops the oldest.
    result = _prioritized_sections(matched=set(), prior_ordered=["a", "b", "c", "d"], protected=None, cap=3)
    assert result == ["a", "b", "c"]
    assert "d" not in result


def test_matched_section_goes_first_and_pushes_out_oldest_prior() -> None:
    result = _prioritized_sections(matched={"e"}, prior_ordered=["a", "b", "c"], protected=None, cap=3)
    assert result[0] == "e"
    assert len(result) == 3
    assert "c" not in result  # oldest prior dropped to make room
    assert set(result) == {"e", "a", "b"}


def test_protected_section_survives_even_when_outside_the_cap() -> None:
    # 'd' owns a pending_action's tool — a bare "yes" next turn needs it,
    # so it must never be shed even though it's the oldest thing in prior.
    result = _prioritized_sections(matched=set(), prior_ordered=["a", "b", "c", "d"], protected="d", cap=3)
    assert "d" in result
    assert set(result) == {"a", "b", "c", "d"}


def test_matched_within_cap_is_unchanged_no_spurious_drop() -> None:
    result = _prioritized_sections(matched={"c"}, prior_ordered=["a", "b"], protected=None, cap=3)
    assert set(result) == {"a", "b", "c"}
    assert len(result) == 3


def test_matched_sections_are_never_dropped_even_if_more_than_cap() -> None:
    result = _prioritized_sections(matched={"a", "b", "c", "d"}, prior_ordered=[], protected=None, cap=3)
    assert set(result) == {"a", "b", "c", "d"}


def test_protected_already_present_is_not_duplicated() -> None:
    result = _prioritized_sections(matched={"a"}, prior_ordered=["b", "c"], protected="a", cap=3)
    assert result.count("a") == 1


def test_default_cap_constant_is_three() -> None:
    assert MAX_ACTIVE_SECTIONS == 3


def test_short_conversation_within_cap_matches_old_behavior() -> None:
    # A conversation that never exceeds the cap should behave exactly as
    # the old unbounded-sticky logic did: everything survives.
    prior: list[str] = []
    for msg_sections in [{"library"}, {"web"}]:
        prior = _prioritized_sections(matched=msg_sections, prior_ordered=prior, protected=None, cap=3)
    assert set(prior) == {"library", "web"}
