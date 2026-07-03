from __future__ import annotations

from seedbox_mcp.tools.host_health import (
    AUTO_RECOVER_SERVICES,
    BEYOND_MEDIA_SERVICES,
    EXCLUDED_INFRA,
    RESTARTABLE_SERVICES,
)


def test_restartable_is_union_of_the_two_tiers() -> None:
    assert RESTARTABLE_SERVICES == AUTO_RECOVER_SERVICES | BEYOND_MEDIA_SERVICES


def test_beyond_media_is_request_only_not_auto_recovered() -> None:
    # The whole point of the split: the monitor must never silently bounce
    # DNS/vault/monitoring. Beyond-media stays out of the auto-recover set.
    assert AUTO_RECOVER_SERVICES.isdisjoint(BEYOND_MEDIA_SERVICES)
    for svc in ("adguardhome", "vaultwarden", "uptime-kuma", "gotify"):
        assert svc in RESTARTABLE_SERVICES
        assert svc not in AUTO_RECOVER_SERVICES


def test_shared_infra_is_never_restartable() -> None:
    # cloudflared/n8n/ollama must not be reachable by either restart path.
    assert EXCLUDED_INFRA.isdisjoint(RESTARTABLE_SERVICES)
    assert EXCLUDED_INFRA.isdisjoint(AUTO_RECOVER_SERVICES)


def test_tdarr_auto_recoverable() -> None:
    # tdarr is media-adjacent and self-contained → safe for unattended recovery.
    assert "tdarr" in AUTO_RECOVER_SERVICES
