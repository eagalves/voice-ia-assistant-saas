"""Tests for Issue #2 — Idempotency-Key Escopada e Semântica de Replay.

Acceptance criteria verified:
AC1 — Request sem Idempotency-Key retorna 400 IDEMPOTENCY_KEY_MISSING
AC2 — Segundo POST idêntico (mesmo payload) dentro de 24h retorna 201 com
       body original e header Idempotency-Replayed: true
AC3 — Segundo POST com mesma chave mas payload diferente retorna 409
       IDEMPOTENCY_PAYLOAD_MISMATCH
AC4 — Chave de tenant A não colide com chave idêntica de tenant B
AC5 — Chave de actor_sub=doctor1 não colide com chave idêntica de actor_sub=doctor2
AC6 — payload_hash é SHA-256 canônico do body (testado com chaves fora de ordem)
AC7 — Registro expirado (> 24h) resulta em nova Sessão criada normalmente
AC8 — Migration Alembic com downgrade() obrigatório
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db.models import IdempotencyKey
from assistente_voz.server.repositories.idempotency_repo import (
    ENDPOINT_CREATE_SESSION,
    IdempotencyRepository,
    TTL_HOURS,
    canonical_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BODY = {
    "session_kind": "medical",
    "actor_role": "doctor",
    "plan": "premium",
    "seniority": None,
}

TENANT_A = DEFAULT_TENANT_ID
TENANT_B = "00000000-0000-0000-0000-000000000002"


def _auth_headers(token: str = "bearer-test", key: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# AC1 — missing Idempotency-Key → 400
# ---------------------------------------------------------------------------


async def test_missing_idempotency_key_returns_400(client: AsyncClient) -> None:
    """AC1: No Idempotency-Key header → 400 IDEMPOTENCY_KEY_MISSING."""
    resp = await client.post(
        "/v1/sessions",
        json=VALID_BODY,
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "IDEMPOTENCY_KEY_MISSING"


async def test_empty_idempotency_key_returns_400(client: AsyncClient) -> None:
    """AC1: Empty Idempotency-Key header also → 400."""
    resp = await client.post(
        "/v1/sessions",
        json=VALID_BODY,
        headers={
            "Authorization": "Bearer test-token",
            "Idempotency-Key": "   ",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "IDEMPOTENCY_KEY_MISSING"


# ---------------------------------------------------------------------------
# AC2 — first call succeeds; duplicate with same payload → replay
# ---------------------------------------------------------------------------


async def test_first_create_session_succeeds(client: AsyncClient) -> None:
    """First POST → 201 with required response fields."""
    idem_key = str(uuid.uuid4())
    resp = await client.post(
        "/v1/sessions",
        json=VALID_BODY,
        headers=_auth_headers(key=idem_key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "session_id" in body
    assert "ws_url" in body
    assert "ws_token" in body
    assert "plan_features" in body
    assert "auto_finalize_at" in body
    # Replay header must NOT be present on first call
    assert "idempotency-replayed" not in resp.headers


async def test_duplicate_same_payload_returns_replay(client: AsyncClient) -> None:
    """AC2: Second POST with same key + same payload → 201 + Idempotency-Replayed: true."""
    idem_key = str(uuid.uuid4())
    headers = _auth_headers(key=idem_key)

    resp1 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp1.status_code == 201
    body1 = resp1.json()

    resp2 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp2.status_code == 201
    assert resp2.headers.get("idempotency-replayed") == "true"

    body2 = resp2.json()
    # Replayed response must have the same session_id as the original
    assert body2["session_id"] == body1["session_id"]
    assert body2["ws_token"] == body1["ws_token"]


async def test_replay_body_identical_to_original(client: AsyncClient) -> None:
    """AC2 detail: replayed body is byte-for-byte identical to original."""
    idem_key = str(uuid.uuid4())
    headers = _auth_headers(key=idem_key)

    resp1 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp1.status_code == 201

    resp2 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp2.status_code == 201
    assert resp1.json() == resp2.json()


# ---------------------------------------------------------------------------
# AC3 — same key, different payload → 409
# ---------------------------------------------------------------------------


async def test_duplicate_different_payload_returns_409(client: AsyncClient) -> None:
    """AC3: Same Idempotency-Key but different payload → 409 IDEMPOTENCY_PAYLOAD_MISMATCH."""
    idem_key = str(uuid.uuid4())
    headers = _auth_headers(key=idem_key)

    resp1 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp1.status_code == 201

    different_body = {**VALID_BODY, "plan": "basico"}
    resp2 = await client.post("/v1/sessions", json=different_body, headers=headers)
    assert resp2.status_code == 409
    assert resp2.json()["error"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


# ---------------------------------------------------------------------------
# AC4 — cross-tenant isolation (repository level)
# ---------------------------------------------------------------------------


async def test_cross_tenant_no_collision(db_engine: AsyncEngine) -> None:
    """AC4: Same key under tenant A and tenant B never collide."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    idem_key = str(uuid.uuid4())
    p_hash = canonical_hash(VALID_BODY)
    fake_response = {"session_id": str(uuid.uuid4())}

    async with sessionmaker() as session_a:
        repo_a = IdempotencyRepository(session_a, TENANT_A)
        record_a = await repo_a.store(
            endpoint=ENDPOINT_CREATE_SESSION,
            actor_sub_jwt="same-actor",
            idempotency_key=idem_key,
            payload_hash=p_hash,
            response_body=fake_response,
            response_status=201,
        )
        await session_a.commit()
        assert record_a is not None

    async with sessionmaker() as session_b:
        repo_b = IdempotencyRepository(session_b, TENANT_B)
        # Tenant B should see no conflict and be able to store the same key.
        record_b = await repo_b.store(
            endpoint=ENDPOINT_CREATE_SESSION,
            actor_sub_jwt="same-actor",
            idempotency_key=idem_key,
            payload_hash=p_hash,
            response_body=fake_response,
            response_status=201,
        )
        await session_b.commit()
        assert record_b is not None
        # They are different rows.
        assert record_a.id != record_b.id


async def test_cross_tenant_get_valid_is_scoped(db_engine: AsyncEngine) -> None:
    """AC4 read-side: tenant A cannot read tenant B's idempotency record."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    idem_key = str(uuid.uuid4())
    p_hash = canonical_hash(VALID_BODY)
    fake_response = {"session_id": str(uuid.uuid4())}

    async with sessionmaker() as session:
        repo_b = IdempotencyRepository(session, TENANT_B)
        await repo_b.store(
            endpoint=ENDPOINT_CREATE_SESSION,
            actor_sub_jwt="actor",
            idempotency_key=idem_key,
            payload_hash=p_hash,
            response_body=fake_response,
            response_status=201,
        )
        await session.commit()

    async with sessionmaker() as session:
        repo_a = IdempotencyRepository(session, TENANT_A)
        result = await repo_a.get_valid(ENDPOINT_CREATE_SESSION, "actor", idem_key)
        # Tenant A must not see tenant B's record
        assert result is None


# ---------------------------------------------------------------------------
# AC5 — cross-actor isolation (HTTP level)
# ---------------------------------------------------------------------------


async def test_cross_actor_no_collision(client: AsyncClient) -> None:
    """AC5: Same key, different bearer tokens → both succeed (no collision)."""
    idem_key = str(uuid.uuid4())

    resp1 = await client.post(
        "/v1/sessions",
        json=VALID_BODY,
        headers=_auth_headers(token="actor-doctor-1", key=idem_key),
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/v1/sessions",
        json=VALID_BODY,
        headers=_auth_headers(token="actor-doctor-2", key=idem_key),
    )
    assert resp2.status_code == 201
    # Both succeed and produce different sessions
    assert resp1.json()["session_id"] != resp2.json()["session_id"]


# ---------------------------------------------------------------------------
# AC6 — canonical hash is order-independent
# ---------------------------------------------------------------------------


def test_canonical_hash_is_order_independent() -> None:
    """AC6: Same keys in different order → same SHA-256 hash."""
    body_a = {"session_kind": "medical", "actor_role": "doctor", "plan": "premium", "seniority": None}
    body_b = {"plan": "premium", "seniority": None, "actor_role": "doctor", "session_kind": "medical"}
    assert canonical_hash(body_a) == canonical_hash(body_b)


def test_canonical_hash_matches_manual_sha256() -> None:
    """AC6 detail: hash matches the spec formula exactly."""
    body = {"session_kind": "medical", "actor_role": "doctor", "plan": "premium", "seniority": None}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert canonical_hash(body) == expected


def test_canonical_hash_differs_on_different_payload() -> None:
    """AC6 sanity: different payload → different hash."""
    body_a = {**VALID_BODY, "plan": "premium"}
    body_b = {**VALID_BODY, "plan": "basico"}
    assert canonical_hash(body_a) != canonical_hash(body_b)


# ---------------------------------------------------------------------------
# AC7 — expired key treated as new
# ---------------------------------------------------------------------------


async def test_expired_key_creates_new_session(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """AC7: Key expired (> 24h) → new session created, not replayed."""
    idem_key = str(uuid.uuid4())
    headers = _auth_headers(key=idem_key)

    resp1 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp1.status_code == 201
    session_id_1 = resp1.json()["session_id"]

    # Manually backdate the expires_at so the record is expired.
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys "
                "SET expires_at = now() - INTERVAL '25 hours' "
                "WHERE idempotency_key = :key"
            ),
            {"key": idem_key},
        )

    # Second POST with same key after expiry → new session.
    resp2 = await client.post("/v1/sessions", json=VALID_BODY, headers=headers)
    assert resp2.status_code == 201
    assert "idempotency-replayed" not in resp2.headers
    # A new session was created (different session_id).
    assert resp2.json()["session_id"] != session_id_1


# ---------------------------------------------------------------------------
# AC8 — migration has downgrade()
# ---------------------------------------------------------------------------


def test_migration_0003_has_downgrade(repo_root) -> None:
    """AC8: Migration file 0003 defines a non-trivial downgrade() function."""
    import ast
    import inspect

    migration_file = (
        repo_root / "migrations" / "versions" / "20260509_0003_idempotency_keys.py"
    )
    assert migration_file.exists(), f"Migration not found: {migration_file}"

    source = migration_file.read_text(encoding="utf-8")
    tree = ast.parse(source)

    downgrade_fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "downgrade"),
        None,
    )
    assert downgrade_fn is not None, "downgrade() not found in migration"
    # Must contain at least one op.* call (not just `pass`)
    has_op_call = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
        for node in ast.walk(downgrade_fn)
    )
    assert has_op_call, "downgrade() in migration 0003 has no op.* calls — must be reversible"
