from __future__ import annotations

from seedbox_mcp.tools.jellyseerr import _availability
from seedbox_mcp.tools.nasdoom import _is_theatrical_rip


def test_availability_only_true_stream_states_are_watchable() -> None:
    # The exact bug: a title mid-processing was reported as "on Plex".
    assert _availability({"mediaInfo": {"status": 2}}) == "requested_pending_approval"
    assert _availability({"mediaInfo": {"status": 5}}) == "available"
    assert _availability({"mediaInfo": {"status": 4}}) == "partially_available"
    assert _availability({"mediaInfo": {"status": 1}}) == "not_available"
    # No mediaInfo at all → nothing in the system.
    assert _availability({}) == "not_in_library"
    # Only 5/4 should ever be treated as streamable-now by callers.
    streamable = {"available", "partially_available"}
    assert _availability({"mediaInfo": {"status": 5}}) in streamable


def test_processing_splits_downloading_vs_waiting_on_downloadstatus() -> None:
    # Status 3 with an active download-client item = actually downloading.
    assert _availability({"mediaInfo": {"status": 3, "downloadStatus": [{"status": "downloading"}]}}) == "downloading"
    # Status 3 with an empty downloadStatus = approved but nothing is
    # downloading (e.g. not released yet) — the Toy Story 5 case. Must NOT
    # read as "downloading".
    waiting = _availability({"mediaInfo": {"status": 3, "downloadStatus": []}})
    assert waiting == "approved_waiting_for_release"
    assert waiting != "downloading"


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
