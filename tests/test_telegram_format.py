from __future__ import annotations

from seedbox_mcp.telegram import format_for_telegram


def test_em_and_en_dashes_stripped() -> None:
    assert "—" not in format_for_telegram("All set — Star Wars has been added.")
    assert format_for_telegram("out in 2027 — not yet") == "out in 2027, not yet"
    # numeric range keeps a hyphen, not a comma
    assert format_for_telegram("trilogy (1977–1983)") == "trilogy (1977-1983)"
    # double-asterisk bold still normalized to single
    assert format_for_telegram("**bold**") == "*bold*"
