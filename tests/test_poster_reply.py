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


def test_extra_poster_markers_stripped_from_text() -> None:
    from seedbox_mcp.telegram_bot_friend import _ANY_POSTER_RE, _POSTER_RE

    r = "A [POSTER:https://image.tmdb.org/t/p/w500/a.jpg] and B [POSTER:https://image.tmdb.org/t/p/w500/b.jpg]"
    assert _POSTER_RE.search(r).group(1) == "https://image.tmdb.org/t/p/w500/a.jpg"
    assert "[POSTER:" not in _ANY_POSTER_RE.sub("", r)


def test_search_query_strips_trailing_year() -> None:
    from seedbox_mcp.tools.jellyseerr import _clean_query

    assert _clean_query("Dream Eater 2025") == "Dream Eater"
    assert _clean_query("The Godfather (1972)") == "The Godfather"
    assert _clean_query("Her") == "Her"
    assert _clean_query("2025") == "2025"  # bare year is not stripped to empty
