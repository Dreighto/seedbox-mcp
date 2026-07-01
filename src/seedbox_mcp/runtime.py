from __future__ import annotations

from dataclasses import dataclass

from seedbox_mcp.clients.arr import ArrClient
from seedbox_mcp.clients.dispatch import DispatchClient
from seedbox_mcp.clients.nasdoom import NasdoomClient
from seedbox_mcp.clients.plex import PlexClient
from seedbox_mcp.clients.sabnzbd import SabnzbdClient
from seedbox_mcp.clients.tautulli import TautulliClient
from seedbox_mcp.config import Settings


@dataclass(frozen=True)
class Services:
    settings: Settings
    radarr: ArrClient
    sonarr: ArrClient
    plex: PlexClient
    tautulli: TautulliClient | None = None
    # Prowlarr and Jellyseerr are both Servarr-family APIs (X-Api-Key header,
    # same request/error shape as Radarr/Sonarr) — reuse ArrClient rather than
    # writing near-identical clients.
    prowlarr: ArrClient | None = None
    sabnzbd: SabnzbdClient | None = None
    jellyseerr: ArrClient | None = None
    nasdoom: NasdoomClient | None = None
    dispatch: DispatchClient | None = None


def build_services(settings: Settings) -> Services:
    tautulli = None
    if settings.tautulli_enabled and settings.tautulli_base_url and settings.tautulli_api_key:
        tautulli = TautulliClient(
            settings.tautulli_base_url,
            settings.tautulli_api_key.get_secret_value(),
        )
    prowlarr = None
    if settings.prowlarr_enabled and settings.prowlarr_base_url and settings.prowlarr_api_key:
        prowlarr = ArrClient(settings.prowlarr_base_url, settings.prowlarr_api_key.get_secret_value())
    sabnzbd = None
    if settings.sabnzbd_enabled and settings.sabnzbd_base_url and settings.sabnzbd_api_key:
        sabnzbd = SabnzbdClient(settings.sabnzbd_base_url, settings.sabnzbd_api_key.get_secret_value())
    jellyseerr = None
    if settings.jellyseerr_enabled and settings.jellyseerr_base_url and settings.jellyseerr_api_key:
        jellyseerr = ArrClient(settings.jellyseerr_base_url, settings.jellyseerr_api_key.get_secret_value())
    nasdoom = None
    if settings.nasdoom_enabled and settings.nasdoom_base_url:
        nasdoom = NasdoomClient(settings.nasdoom_base_url)
    dispatch = None
    if settings.dispatch_enabled and settings.dispatch_hmac_secret:
        dispatch = DispatchClient(
            settings.dispatch_listener_url,
            settings.dispatch_hmac_secret.get_secret_value(),
            settings.dispatch_prompt_inbox,
        )
    return Services(
        settings=settings,
        radarr=ArrClient(settings.radarr_base_url, settings.radarr_api_key.get_secret_value()),
        sonarr=ArrClient(settings.sonarr_base_url, settings.sonarr_api_key.get_secret_value()),
        plex=PlexClient(settings.plex_base_url, settings.plex_token.get_secret_value(), settings.plex_verify_tls),
        tautulli=tautulli,
        prowlarr=prowlarr,
        sabnzbd=sabnzbd,
        jellyseerr=jellyseerr,
        nasdoom=nasdoom,
        dispatch=dispatch,
    )
