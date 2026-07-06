from seedbox_mcp.monitor import _notes_to_findings


def test_notes_become_autofixed_findings():
    out = _notes_to_findings("Queue was paused, resumed it.", None, "Restarted tautulli, verified up.")
    assert len(out) == 2
    assert all(f.auto_fixed for f in out)
    assert all(f.severity == "watch" for f in out)
    titles = " ".join(f.title for f in out).lower()
    assert "queue" in titles or "resumed" in titles


def test_notes_empty_gives_no_findings():
    assert _notes_to_findings(None, None, None) == []
