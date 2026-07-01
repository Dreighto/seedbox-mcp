from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEYS = ("TOKEN", "API_KEY", "PASSWORD", "SECRET", "AUTHORIZATION")


def redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if any(marker in key.upper() for marker in SECRET_KEYS):
        return "********"
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 17432
    mcp_public_base_url: HttpUrl | None = None
    mcp_bearer_token: SecretStr = Field(min_length=1)

    radarr_url: HttpUrl
    radarr_api_key: SecretStr = Field(min_length=1)
    radarr_default_root_folder: str = Field(min_length=1)
    radarr_default_quality_profile_id: int = Field(gt=0)
    radarr_default_min_availability: str = "released"

    sonarr_url: HttpUrl
    sonarr_api_key: SecretStr = Field(min_length=1)
    sonarr_default_root_folder: str = Field(min_length=1)
    sonarr_default_quality_profile_id: int = Field(gt=0)
    sonarr_default_language_profile_id: int | None = None
    sonarr_default_series_type: str = "standard"

    plex_url: HttpUrl
    plex_token: SecretStr = Field(min_length=1)
    plex_verify_tls: bool = True
    plex_movie_section: str = "Movies"
    plex_tv_section: str = "TV Shows"

    tautulli_enabled: bool = False
    tautulli_url: HttpUrl | None = None
    tautulli_api_key: SecretStr | None = None

    prowlarr_enabled: bool = False
    prowlarr_url: HttpUrl | None = None
    prowlarr_api_key: SecretStr | None = None

    sabnzbd_enabled: bool = False
    sabnzbd_url: HttpUrl | None = None
    sabnzbd_api_key: SecretStr | None = None

    jellyseerr_enabled: bool = False
    jellyseerr_url: HttpUrl | None = None
    jellyseerr_api_key: SecretStr | None = None

    # NASDOOM's BFF (~/dev/nasdoom) — tailnet-private, no auth needed at this
    # edge. Prefer this for anything it already consolidates (queue, requests,
    # storage-with-denominator, cross-source search) over the raw per-service
    # tools above.
    nasdoom_enabled: bool = False
    nasdoom_url: HttpUrl | None = None

    # NAS Ops bot (@nas_doombot) — operator-only, separate identity from
    # NASDOOM's build-bot. Deliberately NOT named TELEGRAM_BOT_TOKEN: that
    # generic name collides with a pre-existing shell-exported var (the Miru
    # Dispatch bot) which pydantic-settings would silently prefer over this
    # file's .env value — confirmed live 2026-06-30, a test digest went to
    # the wrong bot. Scoped name avoids any future collision on this machine.
    # nas_ops_telegram_allowed_chat_id is a hard allowlist: the polling bot
    # silently ignores any message from a different chat, so finding the bot
    # username on Telegram doesn't get you a reply.
    nas_ops_telegram_bot_token: SecretStr | None = None
    nas_ops_telegram_allowed_chat_id: int | None = None

    # Escalation path — hands issues beyond the harness's own Tier 1 tools to
    # the LogueOS worker dispatch system (same mechanism as the operator's
    # dispatch-worker skill). Secret copied from LogueOS-Orchestrator/.env
    # rather than read cross-repo, matching the credential-duplication
    # convention already used elsewhere in this stack (e.g. the NASDOOM
    # build-bot's telegram.env copied to two hosts).
    dispatch_enabled: bool = False
    dispatch_listener_url: str = "http://127.0.0.1:19100"
    dispatch_hmac_secret: SecretStr | None = None
    dispatch_prompt_inbox: str = "/home/dreighto/dev/LogueOS-Orchestrator/data/n8n_inbox"

    oauth_access_token_ttl: int = Field(default=3600, gt=0)
    oauth_state_path: Path = Path(".oauth_state.json")

    @field_validator("sonarr_default_language_profile_id", mode="before")
    @classmethod
    def empty_int_is_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @field_validator(
        "tautulli_api_key",
        "prowlarr_api_key",
        "sabnzbd_api_key",
        "jellyseerr_api_key",
        "nas_ops_telegram_bot_token",
        "dispatch_hmac_secret",
        mode="before",
    )
    @classmethod
    def empty_secret_is_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    def redacted_summary(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return {key: redact_value(key, value) for key, value in data.items()}

    @property
    def radarr_base_url(self) -> str:
        return str(self.radarr_url).rstrip("/")

    @property
    def sonarr_base_url(self) -> str:
        return str(self.sonarr_url).rstrip("/")

    @property
    def plex_base_url(self) -> str:
        return str(self.plex_url).rstrip("/")

    @property
    def tautulli_base_url(self) -> str | None:
        return str(self.tautulli_url).rstrip("/") if self.tautulli_url else None

    @property
    def prowlarr_base_url(self) -> str | None:
        return str(self.prowlarr_url).rstrip("/") if self.prowlarr_url else None

    @property
    def sabnzbd_base_url(self) -> str | None:
        return str(self.sabnzbd_url).rstrip("/") if self.sabnzbd_url else None

    @property
    def jellyseerr_base_url(self) -> str | None:
        return str(self.jellyseerr_url).rstrip("/") if self.jellyseerr_url else None

    @property
    def nasdoom_base_url(self) -> str | None:
        return str(self.nasdoom_url).rstrip("/") if self.nasdoom_url else None

    def secret(self, name: str) -> str:
        value = getattr(self, name)
        if not isinstance(value, SecretStr):
            raise TypeError(f"{name} is not a SecretStr")
        return value.get_secret_value()


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
