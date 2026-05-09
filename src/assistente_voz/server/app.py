from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .drain import get_drain_manager, reset_drain_manager
from .errors import register_exception_handlers
from .middleware.auth import BearerPresenceMiddleware
from .middleware.version import ApiVersionMiddleware
from .routers.audio import router as audio_router
from .routers.drafts import router as drafts_router
from .routers.sessions import router as sessions_router

API_TITLE = "Assistente de Voz — Componente Telemedicina"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "API HTTP do componente assistente_de_voz consumida pelo app de telemedicina "
    "(Controlador LGPD). Fase 2.0 — single-tenant operacional, multi-tenant ready."
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Install SIGTERM handler on startup; reset drain state on teardown."""
    dm = get_drain_manager()
    loop = asyncio.get_event_loop()
    dm.install_sigterm_handler(loop)
    try:
        yield
    finally:
        reset_drain_manager()


def create_app() -> FastAPI:
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=_lifespan,
    )

    # Order matters: outer middleware runs first on the way in.
    # Auth check must run BEFORE handlers; the version header should be on
    # every response (including 401s emitted by auth). Starlette runs the
    # last-added middleware OUTERMOST, so add version last.
    app.add_middleware(BearerPresenceMiddleware)
    app.add_middleware(ApiVersionMiddleware)

    register_exception_handlers(app)

    app.include_router(sessions_router)
    app.include_router(drafts_router)
    app.include_router(audio_router)

    @app.get("/health", tags=["meta"], include_in_schema=False)
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
