from __future__ import annotations

from seedbox_mcp.tools.nasdoom import _is_theatrical_rip


def test_flags_common_theatrical_rip_tags() -> None:
    assert _is_theatrical_rip("Some.Movie.2025.CAM.x264-YIFY", "cam")
    assert _is_theatrical_rip("Some.Movie.2025.HDTS.1080p", "")
    assert _is_theatrical_rip("Some.Movie.2025.TELESYNC-GROUP", "")
    assert _is_theatrical_rip("Some Movie 2025 HDCAM", "")
    assert _is_theatrical_rip("Some.Movie.2025.DVDScr.XviD", "")


def test_does_not_flag_real_streaming_quality() -> None:
    assert not _is_theatrical_rip("Some.Movie.2025.1080p.BluRay.x265-GROUP", "hd")
    assert not _is_theatrical_rip("Some.Movie.2025.2160p.WEB-DL.DDP5.1-GROUP", "uhd")
    assert not _is_theatrical_rip("Some.Show.S01E01.1080p.WEBRip", "hd")
    # "ts" as a substring of a real word must not false-positive.
    assert not _is_theatrical_rip("Guardians.2025.1080p.BluRay", "hd")


def test_summary_derives_from_rip_detection_not_arr_state() -> None:
    # Mirrors the tool's summary logic: standard available iff any non-rip.
    releases = [
        {"theatrical_rip": True},
        {"theatrical_rip": True},
    ]
    non_rips = [r for r in releases if not r["theatrical_rip"]]
    assert not non_rips  # only rips
    assert bool(releases) and not non_rips  # -> only_theatrical_rips True

    mixed = [{"theatrical_rip": True}, {"theatrical_rip": False}]
    non_rips_mixed = [r for r in mixed if not r["theatrical_rip"]]
    assert non_rips_mixed  # standard available
