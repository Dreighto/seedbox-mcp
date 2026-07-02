from __future__ import annotations

from typing import Any

import pytest

from seedbox_mcp import download_strikes
from seedbox_mcp.download_strikes import (
    STRIKE_THRESHOLD,
    classify_queue_item,
    run_download_strike_check,
    update_strikes,
)


def _stalled(download_id: str = "abc") -> dict:
    return {
        "_source": "radarr",
        "_title": "Some Movie",
        "id": 10,
        "downloadId": download_id,
        "status": "warning",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "downloading",
        "errorMessage": "The download is stalled with no connections",
    }


def _healthy(download_id: str = "ok1") -> dict:
    return {
        "_source": "radarr",
        "_title": "Healthy Movie",
        "id": 11,
        "downloadId": download_id,
        "status": "downloading",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
    }


def _import_stuck(download_id: str = "imp1") -> dict:
    return {
        "_source": "sonarr",
        "_title": "Some Show",
        "id": 12,
        "downloadId": download_id,
        "status": "warning",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [{"title": "x", "messages": ["Found archive file, might need to be extracted"]}],
    }


def test_classify_distinguishes_stall_import_healthy() -> None:
    assert classify_queue_item(_stalled())[0] == "stalled"
    assert classify_queue_item(_import_stuck())[0] == "import_issue"
    assert classify_queue_item(_healthy())[0] is None


def test_permission_import_error_is_import_issue_not_stalled() -> None:
    item = {
        "trackedDownloadState": "importFailed",
        "trackedDownloadStatus": "error",
        "status": "warning",
        "errorMessage": "Permission denied when accessing the download path",
    }
    assert classify_queue_item(item)[0] == "import_issue"


def test_strike_accrues_and_only_acts_at_threshold() -> None:
    state: dict = {}
    item = _stalled()
    # Below threshold: no action across the first STRIKE_THRESHOLD-1 cycles.
    for cycle in range(1, STRIKE_THRESHOLD):
        state, to_act, imports = update_strikes(state, [item], now_ts=float(cycle))
        assert to_act == [], f"acted too early at cycle {cycle}"
        assert state["radarr:abc"]["strikes"] == cycle
    # Threshold cycle: it acts exactly once.
    state, to_act, imports = update_strikes(state, [item], now_ts=99.0)
    assert len(to_act) == 1
    assert to_act[0]["_strikes"] == STRIKE_THRESHOLD


def test_recovered_download_loses_its_strikes() -> None:
    state: dict = {}
    stalled = _stalled()
    state, _, _ = update_strikes(state, [stalled], now_ts=1.0)
    state, _, _ = update_strikes(state, [stalled], now_ts=2.0)
    assert state["radarr:abc"]["strikes"] == 2
    # Next cycle the same download is healthy again → its entry is dropped,
    # strikes reset, so it can never cross the threshold from stale history.
    recovered = {**stalled, "status": "downloading", "trackedDownloadStatus": "ok", "errorMessage": ""}
    state, to_act, _ = update_strikes(state, [recovered], now_ts=3.0)
    assert "radarr:abc" not in state
    assert to_act == []


def test_import_issue_is_reported_never_struck_or_acted() -> None:
    state: dict = {}
    imp = _import_stuck()
    for cycle in range(1, STRIKE_THRESHOLD + 2):
        state, to_act, imports = update_strikes(state, [imp], now_ts=float(cycle))
        assert to_act == [], "import issues must never be auto-acted"
        assert state == {}, "import issues must not accrue strikes"
        assert len(imports) == 1


def test_item_without_download_id_is_skipped() -> None:
    item = {**_stalled(), "downloadId": None}
    state, to_act, imports = update_strikes({}, [item], now_ts=1.0)
    assert state == {} and to_act == []


class _FakeArr:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.deletes: list[tuple[str, dict[str, Any]]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"records": self._records}

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        self.deletes.append((path, params or {}))


class _FakeServices:
    def __init__(self, radarr: _FakeArr) -> None:
        self.radarr = radarr
        self.sonarr = None


@pytest.mark.asyncio
async def test_end_to_end_acts_only_at_threshold_with_correct_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Full path: a stalled download accrues strikes across cycles and is
    removed with the exact arr DELETE flags (removeFromClient+blocklist+
    re-search) only once it crosses the threshold — never before."""
    raw = {
        "id": 55,
        "downloadId": "hash-xyz",
        "title": "Some.Movie.2024.1080p",
        "movie": {"title": "Some Movie"},
        "status": "warning",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "downloading",
        "errorMessage": "The download is stalled with no connections",
    }
    fake = _FakeArr([raw])
    monkeypatch.setattr(download_strikes, "build_services", lambda settings: _FakeServices(fake))
    monkeypatch.setattr(download_strikes, "rate_limit_exceeded", lambda: False)
    monkeypatch.setattr(download_strikes, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(download_strikes, "STRIKE_STATE_PATH", tmp_path / "strikes.json")

    for cycle in range(1, STRIKE_THRESHOLD):
        note = await run_download_strike_check(settings=None, now_ts=float(cycle))
        assert fake.deletes == [], f"acted too early at cycle {cycle}"
        assert note is None

    note = await run_download_strike_check(settings=None, now_ts=99.0)
    assert len(fake.deletes) == 1
    path, params = fake.deletes[0]
    assert path == "/api/v3/queue/55"
    assert params == {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"}
    assert note is not None and "Some Movie" in note


@pytest.mark.asyncio
async def test_end_to_end_never_acts_on_import_issue(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    raw = {
        "id": 77,
        "downloadId": "imp-hash",
        "title": "Some.Show.S01",
        "series": {"title": "Some Show"},
        "status": "warning",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importFailed",
        "errorMessage": "Permission denied",
    }
    fake = _FakeArr([raw])
    monkeypatch.setattr(download_strikes, "build_services", lambda settings: _FakeServices(fake))
    monkeypatch.setattr(download_strikes, "rate_limit_exceeded", lambda: False)
    monkeypatch.setattr(download_strikes, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(download_strikes, "STRIKE_STATE_PATH", tmp_path / "strikes.json")

    for cycle in range(1, STRIKE_THRESHOLD + 3):
        note = await run_download_strike_check(settings=None, now_ts=float(cycle))
        assert fake.deletes == [], "import issues must never trigger a removal"
        assert note is not None and "import" in note.lower()
