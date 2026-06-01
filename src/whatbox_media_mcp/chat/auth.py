from __future__ import annotations

import logging
from typing import Any, cast

import httpx
from itsdangerous import BadSignature, URLSafeSerializer
from plexapi.myplex import MyPlexAccount
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from whatbox_media_mcp.chat.config import ChatSettings

logger = logging.getLogger("whatbox_chat.auth")

_PLEX_API = "https://plex.tv/api/v2"
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


def verify_server_access(user_token: str, admin_plex_token: str) -> str | None:
    try:
        admin_account = MyPlexAccount(token=admin_plex_token)  # type: ignore[no-untyped-call]
        user_account = MyPlexAccount(token=user_token)  # type: ignore[no-untyped-call]
        username = user_account.username
        if username == admin_account.username:
            return str(username)
        friend_names = {u.username for u in admin_account.users()}  # type: ignore[no-untyped-call]
        if username in friend_names:
            return str(username)
        return None
    except Exception:
        logger.exception("Plex server access verification failed")
        return None


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_ERROR_MESSAGES = {
    "pin_failed": "Could not start Plex sign-in. Please try again.",
    "unauthorized": "Your Plex account does not have access to this server.",
}

_ERROR_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whatbox Media – Sign in</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:#f5f5f5;display:flex;align-items:center;justify-content:center;height:100dvh;margin:0}}
.card{{background:#fff;border-radius:12px;box-shadow:0 0 24px rgba(0,0,0,.08);
padding:40px 48px;max-width:360px;text-align:center}}
h1{{font-size:1rem;font-weight:600;margin-bottom:12px}}
p{{color:#555;font-size:.9rem;margin-bottom:24px}}
a{{display:inline-block;background:#1a1a1a;color:#fff;text-decoration:none;
border-radius:8px;padding:10px 24px;font-size:.9rem}}
a:hover{{background:#333}}
</style></head>
<body><div class="card">
<h1>Sign-in failed</h1>
<p>{message}</p>
<a href="/auth/login">Try again</a>
</div></body></html>"""

_PIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whatbox Media – Sign in</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:#f5f5f5;display:flex;align-items:center;justify-content:center;height:100dvh;margin:0}}
.card{{background:#fff;border-radius:12px;box-shadow:0 0 24px rgba(0,0,0,.08);
padding:40px 48px;max-width:400px;text-align:center}}
h1{{font-size:1rem;font-weight:600;margin-bottom:16px}}
p{{color:#555;font-size:.9rem;margin-bottom:8px}}
a{{color:#1a1a1a;font-weight:600;text-decoration:none}}
a:hover{{text-decoration:underline}}
.pin{{font-size:2.5rem;font-weight:700;letter-spacing:0.2em;margin:20px 0;
color:#1a1a1a;font-variant-numeric:tabular-nums}}
.status{{font-size:.85rem;color:#888;margin-top:20px;min-height:1.2em}}
.error{{color:#c0392b}}
</style></head>
<body><div class="card">
<h1>Sign in with Plex</h1>
<p>Go to <a href="https://www.plex.tv/link/" target="_blank">plex.tv/link</a> and enter:</p>
<div class="pin">{code}</div>
<p class="status" id="status">Waiting for authorisation…</p>
</div>
<script>
async function poll() {{
  let r, d;
  try {{
    r = await fetch('/auth/check');
    d = await r.json();
  }} catch {{ setTimeout(poll, 2000); return; }}
  if (d.ok) {{ window.location.href = '/chat'; return; }}
  if (d.error) {{
    const s = document.getElementById('status');
    s.textContent = d.error;
    s.className = 'status error';
    return;
  }}
  setTimeout(poll, 2000);
}}
setTimeout(poll, 2000);
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def login_handler(request: Request, settings: ChatSettings) -> Response:
    error = request.query_params.get("error")
    if error:
        message = _ERROR_MESSAGES.get(error, "An unexpected error occurred.")
        return HTMLResponse(_ERROR_PAGE.format(message=message), status_code=200)

    try:
        pin_id, pin_code = await create_pin(settings)
    except Exception:
        logger.exception("Failed to create Plex PIN")
        return RedirectResponse("/auth/login?error=pin_failed", status_code=302)

    logger.info("Created PIN %s (id=%d)", pin_code, pin_id)
    pin_serializer = URLSafeSerializer(settings.chat_session_secret.get_secret_value(), salt="pin")
    pin_cookie_value = pin_serializer.dumps({"id": pin_id})

    response = HTMLResponse(_PIN_PAGE.format(code=pin_code))
    response.set_cookie(_PIN_COOKIE, pin_cookie_value, httponly=True, samesite="lax", max_age=300)
    return response


async def check_handler(request: Request, settings: ChatSettings) -> Response:
    pin_cookie_value = request.cookies.get(_PIN_COOKIE, "")
    pin_serializer = URLSafeSerializer(settings.chat_session_secret.get_secret_value(), salt="pin")
    try:
        pin_data: dict[str, Any] = pin_serializer.loads(pin_cookie_value)
        pin_id = int(pin_data["id"])
    except Exception:
        return JSONResponse({"ok": False, "error": "Session expired. Please refresh the page."})

    headers = _plex_headers(settings.chat_plex_client_id)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{_PLEX_API}/pins/{pin_id}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Plex PIN check failed for pin_id=%d", pin_id)
            return JSONResponse({"ok": False})

    token = data.get("authToken")
    if not token:
        return JSONResponse({"ok": False})

    logger.debug("PIN %d linked, verifying server access", pin_id)
    username = verify_server_access(str(token), settings.plex_token.get_secret_value())
    if not username:
        logger.warning("PIN %d: user has no server access", pin_id)
        return JSONResponse({"ok": False, "error": "Your Plex account doesn't have access to this server."})

    logger.info("User %s authenticated via Plex PIN", username)
    session_value = make_session(username, settings.chat_session_secret.get_secret_value())
    response = JSONResponse({"ok": True})
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
        path = request.url.path
        if not (path.startswith("/api/") or path.startswith("/chat")):
            return cast(Response, await call_next(request))

        secret = self._settings.chat_session_secret.get_secret_value()
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        username = read_session(cookie, secret)
        if not username:
            return RedirectResponse("/auth/login", status_code=302)

        request.state.plex_username = username
        return cast(Response, await call_next(request))
