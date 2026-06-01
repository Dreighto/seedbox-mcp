from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeSerializer
from plexapi.myplex import MyPlexAccount
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from whatbox_media_mcp.chat.config import ChatSettings

logger = logging.getLogger("whatbox_chat.auth")

_PLEX_API = "https://plex.tv/api/v2"
_PLEX_AUTH_URL = "https://app.plex.tv/auth#"
_SESSION_COOKIE = "plex_session"
_PIN_COOKIE = "plex_pin"


# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def make_session(username: str, secret: str) -> str:
    s = URLSafeSerializer(secret, salt="session")
    return s.dumps({"u": username})


def read_session(cookie: str, secret: str) -> str | None:
    if not cookie:
        return None
    try:
        s = URLSafeSerializer(secret, salt="session")
        data = s.loads(cookie)
        return str(data["u"])
    except (BadSignature, KeyError, Exception):
        return None


# ---------------------------------------------------------------------------
# Plex PIN flow
# ---------------------------------------------------------------------------


def _plex_headers(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": "Whatbox Chat",
        "Accept": "application/json",
    }


async def create_pin(settings: ChatSettings) -> tuple[int, str]:
    headers = _plex_headers(settings.chat_plex_client_id)
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{_PLEX_API}/pins", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return int(data["id"]), str(data["code"])


async def poll_pin(pin_id: int, settings: ChatSettings, retries: int = 8, delay: float = 1.0) -> str | None:
    headers = _plex_headers(settings.chat_plex_client_id)
    async with httpx.AsyncClient() as client:
        for _ in range(retries):
            resp = await client.get(f"{_PLEX_API}/pins/{pin_id}", headers=headers)
            resp.raise_for_status()
            token = resp.json().get("authToken")
            if token:
                return str(token)
            if delay:
                await asyncio.sleep(delay)
    return None


def verify_server_access(user_token: str, admin_plex_token: str) -> str | None:
    try:
        admin_account = MyPlexAccount(token=admin_plex_token)
        friends = admin_account.users()
        friend_names = {u.username for u in friends}
        user_account = MyPlexAccount(token=user_token)
        username = user_account.username
        if username in friend_names:
            return username
        return None
    except Exception:
        logger.exception("Plex server access verification failed")
        return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def login_handler(request: Request, settings: ChatSettings) -> Response:
    try:
        pin_id, pin_code = await create_pin(settings)
    except Exception:
        logger.exception("Failed to create Plex PIN")
        return RedirectResponse("/auth/login?error=pin_failed", status_code=302)

    pin_serializer = URLSafeSerializer(settings.chat_session_secret.get_secret_value(), salt="pin")
    pin_cookie_value = pin_serializer.dumps({"id": pin_id})

    callback_url = f"{settings.chat_public_base_url.rstrip('/')}/auth/callback"
    plex_params = urlencode(
        {
            "clientID": settings.chat_plex_client_id,
            "code": pin_code,
            "forwardUrl": callback_url,
        }
    )
    redirect_url = f"{_PLEX_AUTH_URL}{plex_params}"

    response = RedirectResponse(redirect_url, status_code=302)
    response.set_cookie(_PIN_COOKIE, pin_cookie_value, httponly=True, samesite="lax", max_age=300)
    return response


async def callback_handler(request: Request, settings: ChatSettings) -> Response:
    pin_cookie_value = request.cookies.get(_PIN_COOKIE, "")
    pin_serializer = URLSafeSerializer(settings.chat_session_secret.get_secret_value(), salt="pin")
    try:
        pin_data: dict[str, Any] = pin_serializer.loads(pin_cookie_value)
        pin_id = int(pin_data["id"])
    except Exception:
        return RedirectResponse("/auth/login?error=invalid_state", status_code=302)

    user_token = await poll_pin(pin_id, settings, retries=8, delay=1.0)
    if not user_token:
        return RedirectResponse("/auth/login?error=timeout", status_code=302)

    username = verify_server_access(user_token, settings.plex_token.get_secret_value())
    if not username:
        return RedirectResponse("/auth/login?error=unauthorized", status_code=302)

    session_value = make_session(username, settings.chat_session_secret.get_secret_value())
    response = RedirectResponse("/chat", status_code=302)
    response.delete_cookie(_PIN_COOKIE)
    response.set_cookie(_SESSION_COOKIE, session_value, httponly=True, samesite="lax")
    return response


async def logout_handler(request: Request) -> Response:
    response = RedirectResponse("/auth/login", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class PlexAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: ChatSettings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        secret = self._settings.chat_session_secret.get_secret_value()
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        username = read_session(cookie, secret)
        if not username:
            return RedirectResponse("/auth/login", status_code=302)

        request.state.plex_username = username
        return await call_next(request)
