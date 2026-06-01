from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from anthropic import AsyncAnthropic
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from whatbox_media_mcp.chat.ai import chat_turn, load_system_prompt
from whatbox_media_mcp.chat.auth import (
    PlexAuthMiddleware,
    callback_handler,
    login_handler,
    logout_handler,
)
from whatbox_media_mcp.chat.config import ChatSettings, load_chat_settings
from whatbox_media_mcp.chat.mcp_client import make_mcp_client

logger = logging.getLogger("whatbox_chat.server")

_STATIC_DIR = Path(__file__).parent / "static"


async def _serve_index(request: Request) -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


async def _chat_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    message: str = body.get("message", "")
    history: list[Any] = body.get("history", [])

    settings: ChatSettings = request.app.state.settings
    reply, updated_history = await chat_turn(
        message=message,
        history=history,
        settings=settings,
        mcp_client=request.app.state.mcp_client,
        anthropic_client=request.app.state.anthropic_client,
    )
    return JSONResponse({"reply": reply, "history": updated_history})


def create_chat_app(settings: ChatSettings) -> Starlette:
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    mcp_client = make_mcp_client(settings)

    async def _login(request: Request) -> object:
        return await login_handler(request, settings)

    async def _callback(request: Request) -> object:
        return await callback_handler(request, settings)

    routes = [
        Route("/", lambda r: RedirectResponse("/chat", status_code=302)),
        Route("/chat", _serve_index),
        Route("/auth/login", _login),
        Route("/auth/callback", _callback),
        Route("/auth/logout", logout_handler, methods=["POST"]),
        Route("/api/chat", _chat_endpoint, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR))),
    ]

    app = Starlette(routes=routes)
    app.add_middleware(PlexAuthMiddleware, settings=settings)
    app.state.settings = settings
    app.state.mcp_client = mcp_client
    app.state.anthropic_client = anthropic_client
    app.state.system_prompt = load_system_prompt(settings)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_chat_settings()
    logger.info("Starting Whatbox Chat on %s:%s", settings.chat_host, settings.chat_port)
    uvicorn.run(create_chat_app(settings), host=settings.chat_host, port=settings.chat_port)


if __name__ == "__main__":
    main()
