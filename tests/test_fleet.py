from __future__ import annotations

import pytest

from seedbox_mcp.clients.uptime_kuma import parse_fleet_metrics
from seedbox_mcp.tools.fleet import fleet_health

SAMPLE = """
# HELP monitor_status ...
monitor_status{monitor_name="Node: NAS",monitor_type="ping",monitor_hostname="100.78.200.6"} 1
monitor_status{monitor_name="Node: ROOM (5060 Ti)",monitor_type="ping",monitor_hostname="100.106.246.89"} 0
monitor_status{monitor_name="Plex",monitor_type="port",monitor_port="32400"} 1
monitor_status{monitor_name="AdGuard (DNS resolving)",monitor_type="dns"} 2
monitor_response_time{monitor_name="Plex"} 12
""".strip()


def test_parse_extracts_name_type_status() -> None:
    mons = parse_fleet_metrics(SAMPLE)
    assert len(mons) == 4  # only monitor_status lines, not response_time
    by = {m["name"]: m for m in mons}
    assert by["Node: NAS"]["up"] is True and by["Node: NAS"]["type"] == "ping"
    assert by["Node: ROOM (5060 Ti)"]["up"] is False
    assert by["Node: ROOM (5060 Ti)"]["status_label"] == "down"
    assert by["AdGuard (DNS resolving)"]["status_label"] == "pending"


def test_parse_ignores_garbage() -> None:
    assert parse_fleet_metrics("not metrics\n# comment\n") == []


class _StubKuma:
    def __init__(self, monitors: list[dict]) -> None:
        self._m = monitors

    async def fleet_status(self) -> list[dict]:
        return self._m


class _StubServices:
    def __init__(self, kuma: object | None) -> None:
        self.uptime_kuma = kuma


@pytest.mark.asyncio
async def test_fleet_health_groups_nodes_vs_services_and_flags_down() -> None:
    monitors = parse_fleet_metrics(SAMPLE)
    res = await fleet_health(_StubServices(_StubKuma(monitors)))  # type: ignore[arg-type]
    data = res.get("data", res)
    assert data["all_ok"] is False
    assert "Node: ROOM (5060 Ti)" in data["down"]
    assert data["nodes"]["total"] == 2 and data["nodes"]["up"] == 1
    assert data["services"]["total"] == 2  # Plex + AdGuard
    assert "AdGuard (DNS resolving)" in data["services"]["not_checked"]


@pytest.mark.asyncio
async def test_fleet_health_not_configured() -> None:
    res = await fleet_health(_StubServices(None))  # type: ignore[arg-type]
    # failure ToolResponse carries the code somewhere in the payload
    assert "not_configured" in str(res)
