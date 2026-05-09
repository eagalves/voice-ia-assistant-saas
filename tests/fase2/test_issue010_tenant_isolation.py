"""Issue #10 — tenant isolation guardrails (CI lint + runtime checks)."""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db.models import Session as SessionRow
from assistente_voz.server.metrics import (
    REQUIRED_METRIC_LABELS,
    assert_labels_include_tenant,
    labels_for,
)
from assistente_voz.server.redis_keys import (
    extract_hash_tag,
    session_events_key,
    session_state_key,
    ws_revocations_channel,
    ws_token_key,
)
from assistente_voz.server.repositories.drafts_repo import DraftsRepository
from assistente_voz.server.repositories.feedback_repo import FeedbackRepository
from assistente_voz.server.repositories.sessions_repo import SessionsRepository

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# AC: TenantRepository scopes every query by tenant_id (runtime).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_query_returns_empty(
    db_engine: AsyncEngine, truncate_tables
) -> None:
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo_a = SessionsRepository(db, TENANT_A)
        created = await repo_a.create(
            session_kind="medical",
            actor_role="doctor",
            plan="premium",
            seniority="senior",
            plan_features={"plan": "premium"},
            idempotency_key="iso-test",
            ws_token=uuid.uuid4(),
        )
        await db.commit()
        captured_id = created.id

    sm_b = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm_b() as db:
        repo_b = SessionsRepository(db, TENANT_B)
        leaked = await repo_b.get(captured_id)
    assert leaked is None, "Tenant B was able to read tenant A's session row."

    async with sm() as db:
        repo_a = SessionsRepository(db, TENANT_A)
        original = await repo_a.get(captured_id)
    assert original is not None
    assert str(original.tenant_id) == TENANT_A


@pytest.mark.asyncio
async def test_repositories_inherit_tenant_repository() -> None:
    from assistente_voz.server.repositories.base import TenantRepository

    for cls in (SessionsRepository, DraftsRepository, FeedbackRepository):
        assert issubclass(cls, TenantRepository), (
            f"{cls.__name__} must inherit from TenantRepository to enforce "
            "the tenant scope contract."
        )


# ---------------------------------------------------------------------------
# AC: every table has tenant_id NOT NULL with the default UUID.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_table_has_tenant_id_not_null(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT table_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND column_name = 'tenant_id'
                    """
                )
            )
        ).all()
    table_to_nullable = {row.table_name: row.is_nullable for row in rows}

    expected = {"sessions", "drafts", "draft_feedback"}
    missing = expected - set(table_to_nullable)
    assert not missing, f"Tables missing tenant_id column: {missing}"
    nullable = [t for t, n in table_to_nullable.items() if n != "NO"]
    assert not nullable, f"Tables with nullable tenant_id: {nullable}"


# ---------------------------------------------------------------------------
# AC: lint — no module imports sqlalchemy.orm.Session outside repositories/.
# ---------------------------------------------------------------------------

SERVER_PKG = Path(__file__).resolve().parents[2] / "src" / "assistente_voz" / "server"
ALLOWED_SESSION_IMPORT_DIRS = {
    SERVER_PKG / "repositories",
    SERVER_PKG / "db",
}


def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imports_forbidden_session(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "sqlalchemy.orm":
                for alias in node.names:
                    if alias.name == "Session":
                        found.append("from sqlalchemy.orm import Session")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy.orm.Session":
                    found.append("import sqlalchemy.orm.Session")
    return found


def test_no_direct_sqlalchemy_orm_session_import_outside_repositories() -> None:
    violations: list[str] = []
    for path in _iter_python_files(SERVER_PKG):
        if any(parent in ALLOWED_SESSION_IMPORT_DIRS for parent in path.parents):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            pytest.fail(f"Could not parse {path}: {exc}")
        for offence in _imports_forbidden_session(tree):
            violations.append(f"{path}: {offence}")
    assert not violations, (
        "Direct `sqlalchemy.orm.Session` import is forbidden outside "
        "`server/repositories/` and `server/db/`. Offenders:\n  - "
        + "\n  - ".join(violations)
    )


# ---------------------------------------------------------------------------
# AC: lint — raw SQL DML in source files must include tenant_id (AST scan).
# Equivalent guarantee to "sqlfluff custom rule" in the issue, with no extra
# infrastructure: catches future regressions where someone bypasses the repo
# layer and writes raw `SELECT ... FROM sessions` etc.
# ---------------------------------------------------------------------------

DML_PATTERN = re.compile(
    r"\b(SELECT\s+.+?\s+FROM|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
    re.IGNORECASE | re.DOTALL,
)
RAW_SQL_DML_ALLOWED_DIRS = {
    SERVER_PKG / "repositories",
    SERVER_PKG / "db",
}


def _find_raw_sql_strings(tree: ast.AST) -> list[str]:
    raw_sql: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if DML_PATTERN.search(value):
                raw_sql.append(value)
    return raw_sql


def test_raw_sql_dml_outside_repos_includes_tenant_id() -> None:
    violations: list[str] = []
    for path in _iter_python_files(SERVER_PKG):
        if any(parent in RAW_SQL_DML_ALLOWED_DIRS for parent in path.parents):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            pytest.fail(f"Could not parse {path}: {exc}")
        for sql in _find_raw_sql_strings(tree):
            if "tenant_id" not in sql.lower():
                snippet = " ".join(sql.split())[:120]
                violations.append(f"{path}: {snippet!r}")
    assert not violations, (
        "Raw SQL DML outside `server/repositories/` and `server/db/` must "
        "include a tenant_id filter. Offenders:\n  - "
        + "\n  - ".join(violations)
    )


# ---------------------------------------------------------------------------
# AC: every Redis key carries `{tenant_id}` as the hash tag.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder,args",
    [
        (session_events_key, (TENANT_A, str(uuid.uuid4()))),
        (session_state_key, (TENANT_A, str(uuid.uuid4()))),
        (ws_token_key, (TENANT_A, str(uuid.uuid4()))),
        (ws_revocations_channel, (TENANT_A,)),
    ],
)
def test_redis_key_includes_tenant_id_hash_tag(builder, args) -> None:
    key = builder(*args)
    tag = extract_hash_tag(key)
    assert tag is not None, f"Key {key} has no hash tag"
    assert TENANT_A in tag, (
        f"Key {key} hash tag {tag!r} does not contain tenant_id {TENANT_A}"
    )


@pytest.mark.parametrize(
    "bad_tenant",
    ["", "not-a-uuid", "1234"],
)
def test_redis_key_rejects_invalid_tenant(bad_tenant: str) -> None:
    with pytest.raises(ValueError):
        session_events_key(bad_tenant, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# AC: metric label sets always include tenant_id.
# ---------------------------------------------------------------------------


def test_labels_for_includes_tenant_id() -> None:
    labels = labels_for(DEFAULT_TENANT_ID, route="/v1/sessions")
    assert labels["tenant_id"] == DEFAULT_TENANT_ID
    assert labels["route"] == "/v1/sessions"
    assert "tenant_id" in REQUIRED_METRIC_LABELS


def test_labels_for_rejects_missing_tenant() -> None:
    with pytest.raises(ValueError):
        labels_for("", route="/v1/sessions")


def test_assert_labels_include_tenant_raises_when_missing() -> None:
    with pytest.raises(AssertionError):
        assert_labels_include_tenant(("route", "method"))
    # Sanity — should NOT raise when tenant_id is present.
    assert_labels_include_tenant(("tenant_id", "route"))


# ---------------------------------------------------------------------------
# AC: RLS migration exists, is reversible, and policies are present.
# ---------------------------------------------------------------------------


def test_rls_migration_file_present_and_reversible(repo_root: Path) -> None:
    candidates = list(
        (repo_root / "migrations" / "versions").glob("*_rls_policies.py")
    )
    assert candidates, "RLS migration file is missing under migrations/versions/"
    body = candidates[0].read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "CREATE POLICY" in body
    assert "DROP POLICY" in body
    assert "DISABLE ROW LEVEL SECURITY" in body


@pytest.mark.asyncio
async def test_rls_policies_applied_in_pg_catalog(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT tablename, policyname
                    FROM pg_policies
                    WHERE schemaname = 'public'
                    """
                )
            )
        ).all()
    by_table = {row.tablename: row.policyname for row in rows}
    for table in ("sessions", "drafts", "draft_feedback"):
        assert table in by_table, (
            f"RLS policy missing for table {table}; pg_policies = {rows}"
        )
        assert by_table[table] == "tenant_isolation"


@pytest.mark.asyncio
async def test_rls_policy_blocks_cross_tenant_when_forced(
    db_engine: AsyncEngine, truncate_tables
) -> None:
    """Defence-in-depth check.

    The dev/test Postgres role (``avoz``) is a superuser, so it bypasses
    RLS regardless of policies. To exercise the policy itself we create
    a transient NOLOGIN role for the duration of the test, GRANT it
    table access, and ``SET LOCAL ROLE`` inside each transaction.
    """
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo_a = SessionsRepository(db, TENANT_A)
        await repo_a.create(
            session_kind="medical",
            actor_role="doctor",
            plan="premium",
            seniority="senior",
            plan_features={"plan": "premium"},
            idempotency_key="rls-test",
            ws_token=uuid.uuid4(),
        )
        await db.commit()

    forced_tables = ("sessions", "drafts", "draft_feedback")
    role = "rls_iso_test_role"

    async with db_engine.begin() as conn:
        await conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        await conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for table in forced_tables:
            await conn.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"
            )
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
            )

    try:
        async with db_engine.begin() as conn:
            await conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)").bindparams(
                    tid=TENANT_B
                )
            )
            count_b = (
                await conn.execute(text("SELECT COUNT(*) FROM sessions"))
            ).scalar_one()
        assert count_b == 0, (
            "RLS+FORCE should hide tenant A rows when app.tenant_id is tenant B"
        )

        async with db_engine.begin() as conn:
            await conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)").bindparams(
                    tid=TENANT_A
                )
            )
            count_a = (
                await conn.execute(text("SELECT COUNT(*) FROM sessions"))
            ).scalar_one()
        assert count_a == 1, (
            "RLS+FORCE should expose tenant A rows when app.tenant_id is tenant A"
        )
    finally:
        async with db_engine.begin() as conn:
            for table in forced_tables:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"
                )
                await conn.exec_driver_sql(
                    f"REVOKE ALL ON {table} FROM {role}"
                )
            await conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")


# ---------------------------------------------------------------------------
# AC: HTTP path remains tenant-scoped end-to-end (uses default tenant in v2.0).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_row_carries_default_tenant_id_via_http(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    payload = {
        "session_kind": "medical",
        "actor_role": "doctor",
        "seniority": "senior",
        "plan": "premium",
    }
    resp = await client.post("/v1/sessions", headers=auth_headers, json=payload)
    assert resp.status_code == 201
    sid = uuid.UUID(resp.json()["session_id"])

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (
            await db.execute(select(SessionRow).where(SessionRow.id == sid))
        ).scalar_one()
    assert str(row.tenant_id) == DEFAULT_TENANT_ID
