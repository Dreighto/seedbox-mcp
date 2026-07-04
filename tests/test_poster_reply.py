from __future__ import annotations

from seedbox_mcp.telegram_bot_friend import _POSTER_RE
from seedbox_mcp.tools.jellyseerr import _poster


def test_poster_marker_only_accepts_tmdb_urls() -> None:
    ok = _POSTER_RE.search("[POSTER:https://image.tmdb.org/t/p/w500/a.jpg] hi")
    assert ok and ok.group(1) == "https://image.tmdb.org/t/p/w500/a.jpg"
    # non-TMDB host is rejected → reply degrades to plain text (no SSRF)
    assert _POSTER_RE.search("[POSTER:https://evil.example.com/a.jpg] hi") is None
    assert _POSTER_RE.search("plain reply, no poster") is None


def test_poster_url_builder() -> None:
    assert _poster("/x.jpg") == "https://image.tmdb.org/t/p/w500/x.jpg"
    assert _poster(None) is None
    assert _poster("notapath") is None
