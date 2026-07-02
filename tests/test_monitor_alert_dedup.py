from __future__ import annotations

from seedbox_mcp.monitor import REMIND_INTERVAL_S, _alert_decision


def test_new_alert_pushes_and_stores() -> None:
    push, state = _alert_decision("abc", {}, now_ts=100.0)
    assert push is True
    assert state == {"hash": "abc", "last_pushed_ts": 100.0}


def test_identical_alert_next_cycle_is_suppressed() -> None:
    prev = {"hash": "abc", "last_pushed_ts": 100.0}
    # 30 minutes later, same alert → stay quiet.
    push, state = _alert_decision("abc", prev, now_ts=100.0 + 1800)
    assert push is False
    assert state == prev  # unchanged


def test_changed_alert_pushes_immediately() -> None:
    prev = {"hash": "abc", "last_pushed_ts": 100.0}
    push, state = _alert_decision("def", prev, now_ts=100.0 + 60)
    assert push is True
    assert state["hash"] == "def"


def test_persistent_alert_reminds_after_interval() -> None:
    prev = {"hash": "abc", "last_pushed_ts": 100.0}
    # Just past the remind interval → re-send once as a reminder.
    push, state = _alert_decision("abc", prev, now_ts=100.0 + REMIND_INTERVAL_S + 1)
    assert push is True
    assert state["last_pushed_ts"] == 100.0 + REMIND_INTERVAL_S + 1


def test_clear_cycle_resets_state() -> None:
    push, state = _alert_decision(None, {"hash": "abc", "last_pushed_ts": 100.0}, now_ts=200.0)
    assert push is False
    assert state == {}  # reset so a later recurrence alerts fresh
