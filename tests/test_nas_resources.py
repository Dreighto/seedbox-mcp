from __future__ import annotations

from seedbox_mcp.tools.host_health import _summarize_resources

HEALTHY = {
    "cores": 12, "load": [1.2, 1.0, 0.8],
    "mem_total_mb": 15000, "mem_available_mb": 12000,
    "swap_total_mb": 4000, "swap_free_mb": 4000, "uptime_s": 360000,
    "top_cpu": [{"cpu": 30.0, "mem": 2.0, "cmd": "ffmpeg"}],
    "top_mem": [{"cpu": 1.0, "mem": 8.0, "cmd": "plex"}],
}


def test_healthy_box_no_flags() -> None:
    s = _summarize_resources(HEALTHY)
    assert s["healthy"] is True
    assert s["pressure"] == ["none"]
    assert s["load_per_core"] == 0.1
    assert s["mem_used_mb"] == 3000
    assert s["mem_used_pct"] == 20.0
    assert s["uptime_hours"] == 100.0


def test_high_load_and_memory_pressure_flagged() -> None:
    raw = dict(HEALTHY, load=[30.0, 28.0, 25.0], mem_available_mb=500)
    s = _summarize_resources(raw)
    assert s["healthy"] is False
    assert "high_load" in s["pressure"]
    assert "memory_pressure" in s["pressure"]


def test_heavy_swap_flagged() -> None:
    raw = dict(HEALTHY, swap_free_mb=1000)  # 3000/4000 used = 75%
    s = _summarize_resources(raw)
    assert "heavy_swap" in s["pressure"]
    assert s["swap_used_mb"] == 3000
