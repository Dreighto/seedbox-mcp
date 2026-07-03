from __future__ import annotations

from seedbox_mcp.clients.tdarr import summarize_tdarr

NODES = {
    "a": {"nodeName": "tdarr-nas-internal", "nodePaused": True, "workers": {}},
    "b": {"nodeName": "tdarr-room-rtx5060ti", "nodePaused": False, "workers": {"w1": {}, "w2": {}}},
}
STATS = [{"sizeDiff": 7_419_000_000, "totalTranscodeCount": 15, "totalHealthCheckCount": 42, "table1Count": 300}]


def test_summarize_nodes_and_stats() -> None:
    s = summarize_tdarr(NODES, STATS)
    assert len(s["nodes"]) == 2
    assert s["any_processing"] is True  # room node not paused / has workers
    assert s["space_saved_gb"] == 7.4
    assert s["lifetime_transcodes"] == 15
    assert s["files_in_library"] == 300


def test_all_paused_no_processing() -> None:
    nodes = {"a": {"nodeName": "x", "nodePaused": True, "workers": {}}}
    s = summarize_tdarr(nodes, [])
    assert s["any_processing"] is False
    assert s["space_saved_gb"] == 0.0
    assert s["lifetime_transcodes"] == 0
