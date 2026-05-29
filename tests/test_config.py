from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from whatbox_media_mcp.config import Settings


def test_config_redacts_secrets(settings: Settings) -> None:
    summary = settings.redacted_summary()
    assert summary["radarr_api_key"] == "********"
    assert summary["sonarr_api_key"] == "********"
    assert summary["plex_token"] == "********"
    assert summary["mcp_bearer_token"] == "********"


def test_config_requires_core_urls_and_credentials() -> None:
    with pytest.raises(ValidationError):
        Settings(mcp_bearer_token=SecretStr("dev"))
