from __future__ import annotations

import pytest
from pydantic import SecretStr

from whatbox_media_mcp.chat.config import ChatSettings


@pytest.fixture
def chat_settings() -> ChatSettings:
    return ChatSettings(  # type: ignore[call-arg]
        mcp_bearer_token=SecretStr("mcp-token"),
        radarr_url="http://radarr.local",
        radarr_api_key=SecretStr("radarr-key"),
        radarr_default_root_folder="/media/Movies",
        radarr_default_quality_profile_id=1,
        sonarr_url="http://sonarr.local",
        sonarr_api_key=SecretStr("sonarr-key"),
        sonarr_default_root_folder="/media/TV",
        sonarr_default_quality_profile_id=1,
        plex_url="http://plex.local",
        plex_token=SecretStr("plex-admin-token"),
        chat_public_base_url="https://example.whatbox.ca/chat",
        chat_session_secret=SecretStr("test-session-secret"),
        chat_plex_client_id="test-plex-client-id",
        anthropic_api_key=SecretStr("sk-ant-test"),
    )
