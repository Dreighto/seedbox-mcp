from __future__ import annotations

from typing import Any

from fastmcp import Client

from whatbox_media_mcp.chat.config import ChatSettings


def make_mcp_client(settings: ChatSettings) -> Client[Any]:
    return Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
