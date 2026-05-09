from __future__ import annotations

from fastapi import FastAPI

from .errors import register_exception_handlers
from .middleware.auth import BearerPresenceMiddleware
from .middleware.version import ApiVersionMiddleware
from .routers.drafts import router as drafts_router
from .routers.sessions import router as sessions_router

API_TITLE = "Assistente de Voz — Componente Telemedicina"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "API HTTP do componente assistente_de_voz consumida pelo app de telemedicina "
    "(Controlador LGPD). Fase 2.0 — single-tenant operacional, multi-tenant ready."
)


def create_app() -> FastAPI:
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
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

    @app.get("/health", tags=["meta"], include_in_schema=False)
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
