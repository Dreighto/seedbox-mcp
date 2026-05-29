from __future__ import annotations

import pytest

from whatbox_media_mcp.server import create_mcp


@pytest.mark.asyncio
async def test_registered_tool_names_exclude_non_goals(services) -> None:  # type: ignore[no-untyped-def]
    mcp = create_mcp(services)
    tools = {tool.name for tool in await mcp.list_tools()}
    assert "media_status" in tools
    assert "media_search" in tools
    assert "torrent_search" not in tools
    assert "prowlarr_search" not in tools
    assert "filesystem_list" not in tools
    assert "shell" not in tools
    assert "plex_delete_item" not in tools
    assert "bulk_delete" not in tools
