"""ASGI entrypoint: `uvicorn app.main:create_app --factory`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import auth_router, router, websocket_endpoint
from .auth import Authenticator
from .config import Settings, get_settings
from .services import build_services

APP_VERSION = "0.1.0"

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class SecurityHeaders:
    """Pure ASGI (not BaseHTTPMiddleware) so endless MJPEG/video streams pass straight through."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        docs = path.startswith("/api/docs")
        static = not path.startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if not docs:
                    headers.setdefault("content-security-policy", CSP)
                if static:
                    # Revalidate the UI's modules on every load (cheap 304s) so updates show up.
                    headers.setdefault("cache-control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_runtime()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request at INFO; with camera polling that would drown everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await build_services(settings)
        app.state.services = services
        await services.start()
        try:
            yield
        finally:
            await services.stop()

    app = FastAPI(
        title="Home Security Dashboard",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.demo_mode else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.demo_mode else None,
    )
    app.state.settings = settings
    app.state.auth = Authenticator(settings)
    app.add_middleware(SecurityHeaders)
    app.include_router(auth_router)
    app.include_router(router)
    app.add_api_websocket_route("/api/ws", websocket_endpoint)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        return RedirectResponse("/favicon.svg", status_code=301)

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
    return app
