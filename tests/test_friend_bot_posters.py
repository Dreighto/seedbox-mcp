from __future__ import annotations

from seedbox_mcp.telegram_bot_friend import split_poster_album, strip_web_sources

A = "https://image.tmdb.org/t/p/w500/a.jpg"
B = "https://image.tmdb.org/t/p/w500/b.jpg"


def test_each_poster_takes_its_own_line_as_caption() -> None:
    reply = (
        f"Could be one of these:\n[POSTER:{A}] *The Martian* (2015). On Plex.\n[POSTER:{B}] *Moon* (2009)\nWhich one?"
    )
    album, rest = split_poster_album(reply)
    assert album == [(A, "*The Martian* (2015). On Plex."), (B, "*Moon* (2009)")]
    assert rest == "Could be one of these:\nWhich one?"


def test_repeats_invalid_urls_and_extras_stay_out_of_the_album() -> None:
    lines = [f"[POSTER:https://image.tmdb.org/t/p/w500/{i}.jpg] pick {i}" for i in range(6)]
    reply = "\n".join([*lines, f"[POSTER:{A}] again", f"[POSTER:{A}] dup", "[POSTER:https://evil.test/x.jpg] fake"])
    album, rest = split_poster_album(reply)
    assert [c for _, c in album] == ["pick 0", "pick 1", "pick 2", "pick 3"]
    assert "POSTER" not in rest and "pick 4" in rest and "fake" in rest


def test_web_sources_are_cut_but_posters_stay() -> None:
    reply = (
        f"Not yet, season 3 is set for 2027 per [Crunchyroll](https://crunchyroll.com/news/x). [POSTER:{A}]\n\n"
        "Sources:\n- [Crunchyroll](https://crunchyroll.com/news/x)\n- [ANN](https://animenewsnetwork.com/y)"
    )
    assert strip_web_sources(reply) == f"Not yet, season 3 is set for 2027 per Crunchyroll. [POSTER:{A}]"
    assert strip_web_sources("*Sources:* none\nfine") == "*Sources:* none\nfine"
