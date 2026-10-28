from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ServerSettings, get_settings
from .db.session import get_sessionmaker
from .drain import get_drain_manager


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped ``AsyncSession``; commits on success, rolls back on error.

    MUST be injected with ``scope="function"`` (see ``DbSession``). Since
    FastAPI 0.121 a yield dependency defaults to ``scope="request"``, whose
    exit code runs *after* the response is sent — and after BackgroundTasks.
    With the default scope the draft materialization worker scheduled by
    ``POST /close`` ran before this commit and never found the draft row.
    ``scope="function"`` exits (commits) right after the handler returns.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db_session, scope="function")]
"""Handler parameter type: ``db: DbSession`` — committed before the response."""


def get_app_settings() -> ServerSettings:
    return get_settings()


def get_bearer_token(request: Request) -> str:
    """Returns the bearer token captured by the middleware."""
    return getattr(request.state, "bearer_token", "")


def get_actor_sub(request: Request) -> str:
    """Returns the JWT ``sub`` claim (or raw Bearer token in bypass mode)."""
    return getattr(request.state, "actor_sub", "")


def get_tenant_id(
    request: Request,
    settings: ServerSettings = Depends(get_app_settings),
) -> str:
    """Derive tenant_id from JWT ``tid`` claim when present; fall back to default.

    In bypass mode (no JWT_JWKS_URL) ``jwt_payload`` is an empty dict so the
    default tenant UUID is used, keeping all pre-Issue-#6 tests green.
    """
    payload: dict = getattr(request.state, "jwt_payload", {})
    tid = payload.get("tid", "")
    return str(tid) if tid else settings.default_tenant_id


def check_not_draining() -> None:
    """Dependency that raises 503 SERVICE_DRAINING when the server is draining.

    Inject into any endpoint that must reject new work after SIGTERM:

        @router.post("/...")
        async def handler(_: None = Depends(check_not_draining), ...):
            ...
    """
    if get_drain_manager().draining:
        raise HTTPException(
            status_code=503,
            detail={"error": "SERVICE_DRAINING", "message": "Server is draining; try again on a different container."},
        )
