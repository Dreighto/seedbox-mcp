from seedbox_mcp.monitor import _notes_to_findings
from seedbox_mcp.triage import fingerprint


def test_notes_become_autofixed_findings():
    out = _notes_to_findings("Queue was paused, resumed it.", None, "Restarted tautulli, verified up.")
    assert len(out) == 2
    assert all(f.auto_fixed for f in out)
    assert all(f.severity == "watch" for f in out)
    titles = " ".join(f.title for f in out).lower()
    assert "queue" in titles or "resumed" in titles


def test_notes_empty_gives_no_findings():
    assert _notes_to_findings(None, None, None) == []


def test_failed_recovery_note_becomes_needs_fix_and_still_alerts():
    note = "tautulli container was down, restart did NOT bring it back; needs escalation."
    out = _notes_to_findings(note)
    assert len(out) == 1
    f = out[0]
    assert f.severity == "needs_fix"
    assert f.auto_fixed is False
    assert fingerprint([f]) is not None


def test_success_note_stays_autofixed_watch():
    out = _notes_to_findings("Queue was paused, resumed it.")
    assert len(out) == 1
    f = out[0]
    assert f.auto_fixed is True
    assert f.severity == "watch"
