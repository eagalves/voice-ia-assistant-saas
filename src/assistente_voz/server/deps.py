from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ServerSettings, get_settings
from .db.session import get_sessionmaker


async def get_db_session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_app_settings() -> ServerSettings:
    return get_settings()


def get_bearer_token(request: Request) -> str:
    """Returns the bearer token captured by the middleware.

    The middleware already returned 401 if absent, so this is safe to call
    from inside a /v1 handler.
    """
    return getattr(request.state, "bearer_token", "")


def get_tenant_id(settings: ServerSettings = Depends(get_app_settings)) -> str:
    """Issue #1: tenant_id is the default UUID. Issue #6 will derive it from
    the JWT (`tid` claim or similar) and Fase 3 will resolve it per-host."""
    return settings.default_tenant_id
