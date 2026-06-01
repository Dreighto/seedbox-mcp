from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from whatbox_media_mcp.chat.auth import (
    PlexAuthMiddleware,
    callback_handler,
    create_pin,
    login_handler,
    logout_handler,
    make_session,
    poll_pin,
    read_session,
    verify_server_access,
)
from whatbox_media_mcp.chat.config import ChatSettings

# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def test_make_and_read_session_roundtrip(chat_settings: ChatSettings) -> None:
    secret = chat_settings.chat_session_secret.get_secret_value()
    token = make_session("mum", secret)
    assert read_session(token, secret) == "mum"


def test_read_session_returns_none_for_tampered_cookie(chat_settings: ChatSettings) -> None:
    secret = chat_settings.chat_session_secret.get_secret_value()
    token = make_session("mum", secret)
    assert read_session(token + "x", secret) is None


def test_read_session_returns_none_for_empty_string(chat_settings: ChatSettings) -> None:
    secret = chat_settings.chat_session_secret.get_secret_value()
    assert read_session("", secret) is None


# ---------------------------------------------------------------------------
# Plex PIN flow — create_pin
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_create_pin_returns_id_and_code(chat_settings: ChatSettings) -> None:
    respx.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 12345, "code": "abc123", "authToken": None})
    )
    pin_id, pin_code = await create_pin(chat_settings)
    assert pin_id == 12345
    assert pin_code == "abc123"


@respx.mock
@pytest.mark.asyncio
async def test_create_pin_sends_client_identifier_header(chat_settings: ChatSettings) -> None:
    route = respx.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 1, "code": "x", "authToken": None})
    )
    await create_pin(chat_settings)
    assert route.called
    assert route.calls[0].request.headers["X-Plex-Client-Identifier"] == "test-plex-client-id"


# ---------------------------------------------------------------------------
# Plex PIN flow — poll_pin
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_poll_pin_returns_token_when_present(chat_settings: ChatSettings) -> None:
    respx.get("https://plex.tv/api/v2/pins/12345").mock(
        return_value=httpx.Response(200, json={"id": 12345, "authToken": "user-plex-token"})
    )
    token = await poll_pin(12345, chat_settings, retries=3, delay=0)
    assert token == "user-plex-token"


@respx.mock
@pytest.mark.asyncio
async def test_poll_pin_returns_none_after_exhausting_retries(chat_settings: ChatSettings) -> None:
    respx.get("https://plex.tv/api/v2/pins/99").mock(
        return_value=httpx.Response(200, json={"id": 99, "authToken": None})
    )
    token = await poll_pin(99, chat_settings, retries=3, delay=0)
    assert token is None


# ---------------------------------------------------------------------------
# verify_server_access
# ---------------------------------------------------------------------------


def test_verify_server_access_returns_username_when_in_friends(chat_settings: ChatSettings) -> None:
    friend = MagicMock()
    friend.username = "mum"

    admin_account = MagicMock()
    admin_account.users.return_value = [friend]

    user_account = MagicMock()
    user_account.username = "mum"

    with patch("whatbox_media_mcp.chat.auth.MyPlexAccount") as MockAccount:
        MockAccount.side_effect = [admin_account, user_account]
        result = verify_server_access("user-token", "admin-token")

    assert result == "mum"


def test_verify_server_access_returns_none_when_not_in_friends(chat_settings: ChatSettings) -> None:
    friend = MagicMock()
    friend.username = "someone_else"

    admin_account = MagicMock()
    admin_account.users.return_value = [friend]

    user_account = MagicMock()
    user_account.username = "mum"

    with patch("whatbox_media_mcp.chat.auth.MyPlexAccount") as MockAccount:
        MockAccount.side_effect = [admin_account, user_account]
        result = verify_server_access("user-token", "admin-token")

    assert result is None


def test_verify_server_access_returns_none_on_exception(chat_settings: ChatSettings) -> None:
    with patch("whatbox_media_mcp.chat.auth.MyPlexAccount", side_effect=Exception("network error")):
        result = verify_server_access("user-token", "admin-token")
    assert result is None


# ---------------------------------------------------------------------------
# Route handlers (via TestClient-style httpx + Starlette)
# ---------------------------------------------------------------------------


def _make_app(chat_settings: ChatSettings):  # type: ignore[no-untyped-def]
    from starlette.applications import Starlette
    from starlette.routing import Route

    async def _login(request):  # type: ignore[no-untyped-def]
        return await login_handler(request, chat_settings)

    async def _callback(request):  # type: ignore[no-untyped-def]
        return await callback_handler(request, chat_settings)

    async def _logout(request):  # type: ignore[no-untyped-def]
        return await logout_handler(request)

    async def _protected(request):  # type: ignore[no-untyped-def]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/auth/login", _login),
            Route("/auth/callback", _callback),
            Route("/auth/logout", _logout, methods=["POST"]),
            Route("/api/chat", _protected, methods=["POST"]),
        ]
    )
    app.add_middleware(PlexAuthMiddleware, settings=chat_settings)
    return app


@respx.mock
@pytest.mark.asyncio
async def test_login_handler_sets_pin_cookie_and_redirects(chat_settings: ChatSettings) -> None:
    respx.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 42, "code": "mycode", "authToken": None})
    )
    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/auth/login")

    assert response.status_code == 302
    location = response.headers["location"]
    assert "app.plex.tv/auth" in location
    assert "mycode" in location
    assert "Set-Cookie" in response.headers


@respx.mock
@pytest.mark.asyncio
async def test_callback_handler_happy_path_sets_session(chat_settings: ChatSettings) -> None:
    from itsdangerous import URLSafeSerializer

    pin_cookie = URLSafeSerializer(chat_settings.chat_session_secret.get_secret_value(), salt="pin").dumps({"id": 7})

    respx.get("https://plex.tv/api/v2/pins/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "authToken": "real-token"})
    )

    friend = MagicMock()
    friend.username = "mum"
    admin_acct = MagicMock()
    admin_acct.users.return_value = [friend]
    user_acct = MagicMock()
    user_acct.username = "mum"

    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    with patch("whatbox_media_mcp.chat.auth.MyPlexAccount") as MockAccount:
        MockAccount.side_effect = [admin_acct, user_acct]
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get(
                "/auth/callback",
                cookies={"plex_pin": pin_cookie},
            )

    assert response.status_code == 302
    assert response.headers["location"] == "/chat"
    assert "plex_session" in response.headers.get("set-cookie", "")


@respx.mock
@pytest.mark.asyncio
async def test_callback_handler_timeout_redirects_to_error(chat_settings: ChatSettings) -> None:
    from itsdangerous import URLSafeSerializer

    pin_cookie = URLSafeSerializer(chat_settings.chat_session_secret.get_secret_value(), salt="pin").dumps({"id": 8})

    respx.get("https://plex.tv/api/v2/pins/8").mock(return_value=httpx.Response(200, json={"id": 8, "authToken": None}))

    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get("/auth/callback", cookies={"plex_pin": pin_cookie})

    assert response.status_code == 302
    assert "error=timeout" in response.headers["location"]


@respx.mock
@pytest.mark.asyncio
async def test_callback_handler_unauthorized_redirects_to_error(chat_settings: ChatSettings) -> None:
    from itsdangerous import URLSafeSerializer

    pin_cookie = URLSafeSerializer(chat_settings.chat_session_secret.get_secret_value(), salt="pin").dumps({"id": 9})

    respx.get("https://plex.tv/api/v2/pins/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "authToken": "real-token"})
    )

    admin_acct = MagicMock()
    admin_acct.users.return_value = []  # no friends
    user_acct = MagicMock()
    user_acct.username = "mum"

    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    with patch("whatbox_media_mcp.chat.auth.MyPlexAccount") as MockAccount:
        MockAccount.side_effect = [admin_acct, user_acct]
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get("/auth/callback", cookies={"plex_pin": pin_cookie})

    assert response.status_code == 302
    assert "error=unauthorized" in response.headers["location"]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_redirects_unauthenticated_request(chat_settings: ChatSettings) -> None:
    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.post("/api/chat", json={"message": "hi"})

    assert response.status_code == 302
    assert "/auth/login" in response.headers["location"]


@pytest.mark.asyncio
async def test_middleware_only_protects_api_paths(chat_settings: ChatSettings) -> None:
    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    with respx.mock:
        respx.post("https://plex.tv/api/v2/pins").mock(
            return_value=httpx.Response(201, json={"id": 1, "code": "c", "authToken": None})
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.get("/auth/login")

    assert response.status_code == 302
    assert "auth/login" not in response.headers["location"]


@pytest.mark.asyncio
async def test_middleware_allows_authenticated_request(chat_settings: ChatSettings) -> None:
    secret = chat_settings.chat_session_secret.get_secret_value()
    session_cookie = make_session("mum", secret)

    app = _make_app(chat_settings)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.post(
            "/api/chat",
            json={"message": "hi"},
            cookies={"plex_session": session_cookie},
        )

    assert response.status_code == 200
