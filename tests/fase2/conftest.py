"""Test fixtures for Fase 2 HTTP API.

Postgres source-of-truth resolution (in priority order):
1. ``TEST_DATABASE_URL`` env (e.g. pointing at ``docker compose`` Postgres)
   — fastest iteration, no container start-up cost.
2. ``testcontainers[postgres]`` — spins a disposable Postgres 16 instance
   on first use. Requires Docker on the host.

If neither is available, the whole module is skipped with a clear message.

Schema is applied via Alembic ``upgrade head`` so RLS policies (Issue #10
migration ``0002_rls_policies``) and any future migration are exercised
end-to-end during tests.

Loop scoping strategy:
- Schema setup is a synchronous session-scoped fixture that internally calls
  ``asyncio.run`` for the pre-clean — sync context dodges the
  "loop already running" trap and keeps the global asyncio config at the
  default (function-scoped tests), so Phase 1 tests are unaffected.
- The async engine is recreated per test (function-scoped). NullPool means
  no connection persists across tests, so each test runs in its own loop
  without leaking handles.
"""

from __future__ import annotations

import asyncio
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

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _to_async_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def _alembic_command(database_url: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = database_url
    cfg = Config(str(ALEMBIC_INI))
    if target == "downgrade-base":
        command.downgrade(cfg, "base")
    else:
        command.upgrade(cfg, target)


async def _drop_all_objects(database_url: str) -> None:
    engine = create_async_engine(database_url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "DROP TABLE IF EXISTS draft_feedback, drafts, sessions, "
                "idempotency_keys, alembic_version CASCADE"
            )
    finally:
        await engine.dispose()


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


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[str]:
    """Sync session-scoped fixture: pre-clean + alembic upgrade head."""
    asyncio.run(_drop_all_objects(database_url))
    _alembic_command(database_url, "head")
    yield database_url
    asyncio.run(_drop_all_objects(database_url))


@pytest_asyncio.fixture
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        migrated_database, future=True, poolclass=NullPool
    )
    db_session_module.override_engine(engine)
    try:
        yield engine
    finally:
        await engine.dispose()
        await db_session_module.reset_engine()


@pytest_asyncio.fixture
async def truncate_tables(db_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with db_engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE draft_feedback, drafts, sessions, idempotency_keys "
            "RESTART IDENTITY CASCADE"
        )


@pytest.fixture
def app(db_engine: AsyncEngine):
    return server_app_module.create_app()


@pytest_asyncio.fixture
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
    return REPO_ROOT
