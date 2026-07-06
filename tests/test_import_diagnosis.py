from __future__ import annotations

import base64
import subprocess

import pytest

from seedbox_mcp import import_diagnosis
from seedbox_mcp.import_diagnosis import _arr_reason, _diagnose_item, _parse_mounts, translate_to_host
from seedbox_mcp.runtime import Services


def test_arr_reason_pulls_the_real_message_from_status_messages() -> None:
    # The live case: a Sonarr importblocked item's real reason lives in
    # statusMessages, not errorMessage.
    item = {
        "errorMessage": "",
        "statusMessages": [
            {"title": "release.name", "messages": ["Series title mismatch; automatic import is not possible."]}
        ],
    }
    reason = _arr_reason(item)
    assert "title mismatch" in reason.lower()
    # And this is what the classifier keys on for match_problem (not perms).
    assert "mismatch" in reason.lower()


def test_arr_reason_empty_when_nothing_reported() -> None:
    assert _arr_reason({}) == ""
    assert _arr_reason({"errorMessage": None, "statusMessages": []}) == ""

# Real radarr mount string shape (host paths include a space in "Anime Movies").
_RADARR_RAW = (
    "/mnt/scratch/downloads/complete=>/downloads/complete "
    "/mnt/scratch/downloads/incomplete=>/downloads/incomplete "
    "/opt/radarr/config=>/config /mnt/pool/Movies=>/movies "
    "/mnt/pool/Anime Movies=>/anime-movies "
)


def test_parse_mounts_handles_spaces_in_host_paths() -> None:
    # _parse_mounts returns (host, container) pairs; index by container.
    by_container = {cont: host for host, cont in _parse_mounts(_RADARR_RAW)}
    assert by_container["/downloads/complete"] == "/mnt/scratch/downloads/complete"
    assert by_container["/movies"] == "/mnt/pool/Movies"
    # The space-containing host path must survive intact.
    assert by_container["/anime-movies"] == "/mnt/pool/Anime Movies"


def test_translate_prefers_longest_container_prefix() -> None:
    mounts = _parse_mounts(_RADARR_RAW)
    # /downloads/complete must win over a hypothetical /downloads mount.
    got = translate_to_host("/downloads/complete/Some.Movie.2024/movie.mkv", mounts)
    assert got == "/mnt/scratch/downloads/complete/Some.Movie.2024/movie.mkv"


def test_translate_maps_library_path_with_space() -> None:
    mounts = _parse_mounts(_RADARR_RAW)
    got = translate_to_host("/anime-movies/Akira (1988)", mounts)
    assert got == "/mnt/pool/Anime Movies/Akira (1988)"


def test_translate_returns_none_for_unmapped_path() -> None:
    mounts = _parse_mounts(_RADARR_RAW)
    assert translate_to_host("/some/unmounted/path", mounts) is None


def test_exact_mount_root_translates() -> None:
    mounts = _parse_mounts(_RADARR_RAW)
    assert translate_to_host("/movies", mounts) == "/mnt/pool/Movies"


@pytest.mark.asyncio
async def test_diagnose_item_probe_command_is_syntactically_valid_shell(
    monkeypatch: pytest.MonkeyPatch, services: Services
) -> None:
    """Regression for the "sh: syntax error: unexpected \"if\"" bug: the
    in-container probe must (a) be delivered via base64 so nested SSH/shell
    quoting can't mangle it, and (b) join its per-mount if/fi blocks with
    ';' — a bare space between two "if ... fi" blocks is itself a shell
    syntax error, independent of the quoting bug.
    """
    captured: dict[str, str] = {}

    async def fake_run_on_nas(_services: object, command: str, timeout: float = 30.0) -> tuple[int, str, str]:
        captured["command"] = command
        return 0, "READ_OK\nPARENT_WRITE_OK\nLIBOK /movies\nLIBOK /anime-movies", ""

    monkeypatch.setattr(import_diagnosis, "_run_on_nas", fake_run_on_nas)

    item = {
        "outputPath": "/downloads/complete/Some.Movie.2024/movie.mkv",
        "movie": {"title": "Some Movie"},
        "errorMessage": "",
        "statusMessages": [],
    }
    result = await _diagnose_item(services, "radarr", item)

    assert result["diagnosis"] == "no_permission_or_path_issue_found"
    command = captured["command"]
    assert "base64 -d | sh" in command

    # Pull the base64 payload back out and confirm it decodes to a script
    # that a real POSIX shell accepts (syntax-check only, `sh -n`, no
    # execution) — this is exactly the check that used to fail with
    # "unexpected \"if\"".
    b64_payload = command.split("echo ", 1)[1].split(" | base64", 1)[0]
    script = base64.b64decode(b64_payload).decode()
    assert "fi if" not in script  # the missing-semicolon bug, reintroduced
    check = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
