from __future__ import annotations
import uuid
from pathlib import Path
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from assistente_voz.server.audit_log import GENESIS_HASH, log_event, verify_chain
from assistente_voz.server.db.models import AuditLog

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def db_session(db_engine):
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


# Each DB test uses a fresh uuid4() tenant_id so tests are naturally isolated
# without needing truncate_tables (which takes ACCESS EXCLUSIVE and can hang).


async def test_ac1_hash_chain_integrity(db_session):
    tenant_id = uuid.uuid4()
    for i in range(3):
        await log_event(
            db_session,
            tenant_id=tenant_id,
            event_type="session_created",
            payload={"index": i},
        )
    await db_session.commit()
    result = await verify_chain(db_session, tenant_id)
    assert result["ok"] is True
    assert result["total"] == 3


async def test_ac2_tamper_detected(db_session):
    tenant_id = uuid.uuid4()
    for i in range(2):
        await log_event(
            db_session,
            tenant_id=tenant_id,
            event_type="draft_generated",
            payload={"step": i},
        )
    await db_session.commit()
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.asc())
        .limit(1)
    )
    first = (await db_session.execute(stmt)).scalar_one()
    first.payload = {"step": 99, "TAMPERED": True}
    await db_session.commit()
    chain = await verify_chain(db_session, tenant_id)
    assert chain["ok"] is False
    assert len(chain["broken"]) >= 1


async def test_ac3_first_event_uses_genesis_hash(db_session):
    tenant_id = uuid.uuid4()
    entry = await log_event(
        db_session,
        tenant_id=tenant_id,
        event_type="session_created",
        payload={"x": 1},
    )
    await db_session.commit()
    assert entry.prev_hash == GENESIS_HASH


async def test_ac4_log_event_stores_correct_tenant_id(db_session):
    tenant_id = uuid.uuid4()
    entry = await log_event(
        db_session,
        tenant_id=tenant_id,
        event_type="login",
        payload={"actor": "nurse-1"},
        actor_sub="nurse-1",
    )
    await db_session.commit()
    assert entry.tenant_id == tenant_id


def test_ac5_dpa_doc_exists():
    p = REPO_ROOT / "docs" / "compliance" / "dpa-internal.md"
    assert p.exists()
    c = p.read_text(encoding="utf-8")
    assert "Controlador" in c and "Operador" in c and "Art. 11" in c


def test_ac6_dpia_doc_has_risk_section():
    p = REPO_ROOT / "docs" / "compliance" / "dpia-componente.md"
    assert p.exists()
    c = p.read_text(encoding="utf-8")
    assert "risco residual" in c.lower()
    assert "Aceite formal" in c


def test_ac7_breach_runbook_exists():
    p = REPO_ROOT / "docs" / "runbooks" / "breach-notification.md"
    assert p.exists()
    c = p.read_text(encoding="utf-8")
    assert "24h" in c
    assert "ANPD" in c
