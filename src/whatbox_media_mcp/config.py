from __future__ import annotations

from typing import Any

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEYS = ("TOKEN", "API_KEY", "PASSWORD", "SECRET", "AUTHORIZATION")


def redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if any(marker in key.upper() for marker in SECRET_KEYS):
        return "********"
    if isinstance(value, SecretStr):
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
    radarr_delete_files_default: bool = False

    sonarr_url: HttpUrl
    sonarr_api_key: SecretStr = Field(min_length=1)
    sonarr_default_root_folder: str = Field(min_length=1)
    sonarr_default_quality_profile_id: int = Field(gt=0)
    sonarr_default_language_profile_id: int | None = None
    sonarr_default_series_type: str = "standard"
    sonarr_delete_files_default: bool = False

    plex_url: HttpUrl
    plex_token: SecretStr = Field(min_length=1)
    plex_movie_section: str = "Movies"
    plex_tv_section: str = "TV Shows"

    tautulli_enabled: bool = False
    tautulli_url: HttpUrl | None = None
    tautulli_api_key: SecretStr | None = None

    @field_validator("sonarr_default_language_profile_id", mode="before")
    @classmethod
    def empty_int_is_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @field_validator("tautulli_api_key", mode="before")
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

    def secret(self, name: str) -> str:
        value = getattr(self, name)
        if not isinstance(value, SecretStr):
            raise TypeError(f"{name} is not a SecretStr")
        return value.get_secret_value()


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
