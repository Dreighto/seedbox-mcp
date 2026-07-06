from seedbox_mcp.monitor import _notes_to_findings
from seedbox_mcp.triage import fingerprint


def test_bundled_fix_and_unresolved_report_splits_into_two_findings():
    note = (
        "Auto-fixed stalled downloads (removed, blocklisted, re-searching) after 2+ cycles "
        "stalled: X.\n"
        '2 download(s) stuck on import, likely a permissions or path issue (NOT auto-fixed, '
        'since re-downloading won\'t fix that): "Y" (stuck importing). Worth a look.'
    )
    out = _notes_to_findings(note)
    assert len(out) == 2

    fix, stuck = out
    assert fix.auto_fixed is True
    assert fix.severity == "watch"

    assert stuck.auto_fixed is False
    assert stuck.severity == "needs_fix"
    assert fingerprint([stuck]) is not None


def test_queue_resume_note_becomes_autofixed_watch():
    note = "Deterministic check: the download queue was paused, resumed it automatically."
    out = _notes_to_findings(note)
    assert len(out) == 1
    f = out[0]
    assert f.auto_fixed is True
    assert f.severity == "watch"


def test_failed_recovery_note_becomes_needs_fix_and_still_alerts():
    note = "tautulli container was down, restart did NOT bring it back; needs escalation."
    out = _notes_to_findings(note)
    assert len(out) == 1
    f = out[0]
    assert f.severity == "needs_fix"
    assert f.auto_fixed is False
    assert fingerprint([f]) is not None


def test_sabnzbd_advisory_note_becomes_needs_fix_and_pushes():
    note = "SABnzbd download history has grown to 58 items, approaching the 60-item window."
    out = _notes_to_findings(note)
    assert len(out) == 1
    f = out[0]
    assert f.severity == "needs_fix"
    assert f.auto_fixed is False
    assert fingerprint([f]) is not None


def test_notes_empty_gives_no_findings():
    assert _notes_to_findings(None, None, None) == []
    assert _notes_to_findings("", "   ", None) == []
