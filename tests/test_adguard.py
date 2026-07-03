from __future__ import annotations

from seedbox_mcp.clients.adguard import summarize_stats

STATUS = {"protection_enabled": True, "running": True}
STATS = {
    "num_dns_queries": 267,
    "num_blocked_filtering": 8,
    "avg_processing_time": 0.021,
    "top_blocked_domains": [{"doubleclick.net": 5}, {"ads.example.com": 3}],
    "top_clients": [{"192.168.50.234": 88}, {"192.168.50.172": 34}],
}


def test_summarize_computes_block_rate_and_flattens_tops() -> None:
    s = summarize_stats(STATS, STATUS)
    assert s["protection_enabled"] is True
    assert s["queries"] == 267 and s["blocked"] == 8
    assert s["block_rate_pct"] == 3.0
    assert s["avg_processing_ms"] == 21.0
    assert s["top_blocked_domains"][0] == {"name": "doubleclick.net", "count": 5}
    assert s["top_clients"][0] == {"name": "192.168.50.234", "count": 88}


def test_summarize_zero_queries_no_divide_by_zero() -> None:
    s = summarize_stats({"num_dns_queries": 0, "num_blocked_filtering": 0}, STATUS)
    assert s["block_rate_pct"] == 0.0
    assert s["top_blocked_domains"] == []
