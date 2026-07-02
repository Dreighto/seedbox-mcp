from __future__ import annotations

from seedbox_mcp.import_diagnosis import _parse_mounts, translate_to_host

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
