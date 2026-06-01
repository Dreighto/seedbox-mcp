from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr

from whatbox_media_mcp.config import Settings


class ChatSettings(Settings):
    chat_host: str = "127.0.0.1"
    chat_port: int = 17433
    chat_public_base_url: str = Field(min_length=1)
    chat_session_secret: SecretStr = Field(min_length=1)
    chat_plex_client_id: str = Field(min_length=1)
    anthropic_api_key: SecretStr = Field(min_length=1)
    system_prompt_path: Path | None = None

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


def load_chat_settings() -> ChatSettings:
    return ChatSettings()  # type: ignore[call-arg]
