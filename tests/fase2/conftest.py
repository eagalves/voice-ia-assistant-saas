"""Test fixtures for Fase 2 HTTP API.

Postgres source-of-truth resolution (in priority order):
1. ``TEST_DATABASE_URL`` env (e.g. pointing at ``docker compose`` Postgres)
   — fastest iteration, no container start-up cost.
2. ``testcontainers[postgres]`` — spins a disposable Postgres 16 instance
   on first use. Requires Docker on the host.

If neither is available, the whole module is skipped with a clear message.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from assistente_voz.server import app as server_app_module
from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db import session as db_session_module
from assistente_voz.server.db.base import Base


def _to_async_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        yield _to_async_url(explicit)
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except Exception:
        pytest.skip(
            "Neither TEST_DATABASE_URL nor testcontainers[postgres] is available; "
            "set TEST_DATABASE_URL or install testcontainers + Docker."
        )
        return

    try:
        container = PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:
        pytest.skip(f"Could not start Postgres container (Docker unavailable?): {exc}")
        return

    try:
        raw = container.get_connection_url()
        if "+psycopg2" in raw:
            raw = raw.replace("+psycopg2", "")
        yield _to_async_url(raw)
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: every checkout opens a fresh asyncpg connection and discards
    # it on release. This avoids "another operation in progress" when the
    # session-scoped engine is shared across tests that may bounce between
    # nested transactions or different async contexts.
    engine = create_async_engine(database_url, future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    db_session_module.override_engine(engine)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        await db_session_module.reset_engine()


@pytest_asyncio.fixture(loop_scope="session")
async def truncate_tables(db_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with db_engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE draft_feedback, drafts, sessions RESTART IDENTITY CASCADE"
        )


@pytest.fixture
def app(db_engine: AsyncEngine):
    return server_app_module.create_app()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app, truncate_tables) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer test-token",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


@pytest.fixture
def default_tenant_id() -> str:
    return DEFAULT_TENANT_ID


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
