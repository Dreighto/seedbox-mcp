from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from whatbox_media_mcp.chat.config import ChatSettings

BASE_REQUIRED = dict(
    mcp_bearer_token=SecretStr("dev"),
    radarr_url="http://radarr.local",
    radarr_api_key=SecretStr("radarr-key"),
    radarr_default_root_folder="/media/Movies",
    radarr_default_quality_profile_id=1,
    sonarr_url="http://sonarr.local",
    sonarr_api_key=SecretStr("sonarr-key"),
    sonarr_default_root_folder="/media/TV",
    sonarr_default_quality_profile_id=1,
    plex_url="http://plex.local",
    plex_token=SecretStr("plex-token"),
    chat_public_base_url="http://localhost:17433",
    chat_session_secret=SecretStr("test-secret"),
    chat_plex_client_id="test-client-id",
    anthropic_api_key=SecretStr("sk-ant-test"),
)


def make_settings(**overrides: object) -> ChatSettings:
    return ChatSettings(**{**BASE_REQUIRED, **overrides})  # type: ignore[arg-type]


def test_chat_settings_loads_with_required_fields() -> None:
    s = make_settings()
    assert s.chat_host == "127.0.0.1"
    assert s.chat_port == 17433
    assert s.chat_plex_client_id == "test-client-id"


def test_mcp_url_derived_from_host_and_port() -> None:
    s = make_settings(mcp_host="127.0.0.1", mcp_port=17432)
    assert s.mcp_url == "http://127.0.0.1:17432/mcp"


def test_system_prompt_path_is_none_by_default() -> None:
    s = make_settings()
    assert s.system_prompt_path is None


def test_system_prompt_path_accepts_valid_path(tmp_path: Path) -> None:
    p = tmp_path / "prompt.txt"
    p.write_text("hello")
    s = make_settings(system_prompt_path=p)
    assert s.system_prompt_path == p


def test_chat_port_default_does_not_clash_with_mcp_port() -> None:
    s = make_settings()
    assert s.chat_port != s.mcp_port
