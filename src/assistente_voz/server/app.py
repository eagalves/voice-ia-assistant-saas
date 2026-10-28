from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from .config import get_settings
from .drain import get_drain_manager, reset_drain_manager
from .errors import register_exception_handlers
from .event_stream import RedisEventStream
from .metrics import render_metrics
from .middleware.auth import BearerPresenceMiddleware
from .middleware.version import ApiVersionMiddleware
from .reaper import SessionReaper
from .routers.audio import router as audio_router
from .routers.drafts import router as drafts_router
from .routers.sessions import router as sessions_router
from .stream_client import get_stream_client

logger = logging.getLogger(__name__)

API_TITLE = "Assistente de Voz — Componente Telemedicina"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "API HTTP do componente assistente_de_voz consumida pelo app de telemedicina "
    "(Controlador LGPD). Fase 2.0 — single-tenant operacional, multi-tenant ready."
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Install SIGTERM handler and start the auto-finalize reaper; tear down on exit."""
    settings = get_settings()
    dm = get_drain_manager()
    dm.install_sigterm_handler(asyncio.get_running_loop())

    reaper_task: asyncio.Task | None = None
    if settings.session_reaper_interval_s > 0:
        reaper = SessionReaper(settings.session_reaper_interval_s)
        reaper_task = asyncio.create_task(reaper.run_forever(), name="session-reaper")
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
            await asyncio.gather(reaper_task, return_exceptions=True)
        reset_drain_manager()


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.is_production and settings.auth_bypass_active:
        # Fail-fast: an unset JWT_JWKS_URL silently accepts any Bearer string.
        raise RuntimeError(
            "CONFIG_AUTH_BYPASS_FORBIDDEN: JWT_JWKS_URL must be set when "
            f"APP_ENV={settings.app_env!r} (production/staging)."
        )

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
        stream = get_stream_client()
        return {
            "status": "ok",
            "event_stream": "redis" if isinstance(stream, RedisEventStream) else "memory",
            "stt_backend": settings.stt_server_backend,
        }

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics_endpoint() -> Response:
        # Scrape endpoint for Prometheus. Not under /v1 → no Bearer required;
        # expose it only on the internal network / ALB listener rule.
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
