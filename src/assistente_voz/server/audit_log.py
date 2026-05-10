"""AuditLog service — Issue #12, HITL Compliance.

Provides an append-only audit log with SHA-256 hash chain for tamper
detection. The hash chain invariant:

  event.event_hash = SHA-256(json(event.payload, sort_keys=True) + prev_hash)

The first event for a tenant uses ``GENESIS_HASH`` as ``prev_hash``.

All DB access goes through ``AsyncSession`` passed by the caller — no
direct engine import — keeping the TenantRepository pattern intact even
though these helpers are not a TenantRepository subclass (they are
thin utilities called from the repository layer or directly from tests).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditLog

GENESIS_HASH = "0" * 64


def compute_hash(payload: dict[str, Any], prev_hash: str) -> str:
    """Return SHA-256 hex of ``json(payload, sort_keys=True) + prev_hash``."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False) + prev_hash
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_last_hash(db: AsyncSession, tenant_id: uuid.UUID | str) -> str:
    """Return the ``event_hash`` of the most recent audit event for *tenant_id*.

    Returns ``GENESIS_HASH`` when the tenant has no events yet.
    """
    tid = uuid.UUID(str(tenant_id)) if not isinstance(tenant_id, uuid.UUID) else tenant_id
    stmt = (
        select(AuditLog.event_hash)
        .where(AuditLog.tenant_id == tid)
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    return row if row is not None else GENESIS_HASH


async def log_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    event_type: str,
    payload: dict[str, Any],
    session_id: uuid.UUID | str | None = None,
    actor_sub: str | None = None,
) -> AuditLog:
    """Append a new audit event and return the persisted instance.

    Computes ``prev_hash`` (last event or GENESIS) and ``event_hash``
    automatically. The caller is responsible for committing the transaction.
    """
    tid = uuid.UUID(str(tenant_id)) if not isinstance(tenant_id, uuid.UUID) else tenant_id
    sid: uuid.UUID | None = None
    if session_id is not None:
        sid = uuid.UUID(str(session_id)) if not isinstance(session_id, uuid.UUID) else session_id

    prev_hash = await get_last_hash(db, tid)
    event_hash = compute_hash(payload, prev_hash)

    entry = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tid,
        event_type=event_type,
        session_id=sid,
        actor_sub=actor_sub,
        payload=payload,
        event_hash=event_hash,
        prev_hash=prev_hash,
    )
    db.add(entry)
    return entry


async def verify_chain(
    db: AsyncSession, tenant_id: uuid.UUID | str
) -> dict[str, Any]:
    """Verify the full hash chain for *tenant_id*.

    Returns a dict::

        {
            "ok": bool,
            "total": int,
            "broken": [{"index": int, "id": str, "expected": str, "got": str}],
        }

    A broken entry means the stored ``event_hash`` does not match
    ``compute_hash(payload, prev_hash_of_row)``.
    """
    tid = uuid.UUID(str(tenant_id)) if not isinstance(tenant_id, uuid.UUID) else tenant_id
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tid)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    result = await db.execute(stmt)
    rows: list[AuditLog] = list(result.scalars().all())

    broken: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        expected = compute_hash(row.payload, row.prev_hash)
        if expected != row.event_hash:
            broken.append({
                "index": i,
                "id": str(row.id),
                "expected": expected,
                "got": row.event_hash,
            })

    return {"ok": len(broken) == 0, "total": len(rows), "broken": broken}
