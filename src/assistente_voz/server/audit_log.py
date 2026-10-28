"""AuditLog service — Issue #12, HITL Compliance (design: ADR 0004).

Append-only audit log with a SHA-256 hash chain for tamper detection.

Hash invariant (per tenant chain)::

    record     = {"event_type", "session_id", "actor_sub", "payload"}
    event_hash = SHA-256(json(record, sort_keys=True) + prev_hash)

The first event of a tenant uses ``GENESIS_HASH`` as ``prev_hash``; every
later event links to the ``event_hash`` of the previous row **in ``seq``
order** (server-assigned identity column — never ``created_at``, which is
the transaction start time and collides inside one transaction).

Concurrency: ``log_event`` takes a per-tenant transaction advisory lock so
two concurrent transactions cannot both read the same ``prev_hash`` and
fork the chain.

``verify_chain`` checks two independent properties:

1. every row's stored ``event_hash`` matches a recomputation over the row
   (detects edits to payload/metadata);
2. every row's ``prev_hash`` equals the previous row's ``event_hash``
   (detects deleted or inserted rows — "gaps").

All DB access goes through the ``AsyncSession`` passed by the caller; the
caller owns the transaction (commit/rollback).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditLog
from .metrics import audit_events_total

GENESIS_HASH = "0" * 64

# ── Canonical event types (keep stable — they are part of the evidence) ──────
EVENT_SESSION_CREATED = "session.created"
EVENT_SESSION_CLOSED = "session.closed"
EVENT_SESSION_AUTO_FINALIZED = "session.auto_finalized"
EVENT_SESSION_TOKEN_REFRESHED = "session.token_refreshed"
EVENT_DRAFT_MATERIALIZED = "draft.materialized"
EVENT_DRAFT_FAILED = "draft.failed"
EVENT_DRAFT_EXPORTED = "draft.exported"
EVENT_DRAFT_RETRY_REQUESTED = "draft.retry_requested"
EVENT_DRAFT_FEEDBACK_RECEIVED = "draft.feedback_received"


def _canonical_record(
    payload: dict[str, Any],
    *,
    event_type: str,
    session_id: uuid.UUID | str | None,
    actor_sub: str | None,
) -> str:
    record = {
        "event_type": event_type,
        "session_id": str(session_id) if session_id is not None else None,
        "actor_sub": actor_sub,
        "payload": payload,
    }
    return json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)


def compute_hash(
    payload: dict[str, Any],
    prev_hash: str,
    *,
    event_type: str = "",
    session_id: uuid.UUID | str | None = None,
    actor_sub: str | None = None,
) -> str:
    """Return SHA-256 hex of the canonical record + ``prev_hash``."""
    raw = _canonical_record(
        payload, event_type=event_type, session_id=session_id, actor_sub=actor_sub
    ) + prev_hash
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def get_last_hash(db: AsyncSession, tenant_id: uuid.UUID | str) -> str:
    """Return the ``event_hash`` of the most recent audit event for *tenant_id*.

    Returns ``GENESIS_HASH`` when the tenant has no events yet.
    """
    tid = _coerce_uuid(tenant_id)
    stmt = (
        select(AuditLog.event_hash)
        .where(AuditLog.tenant_id == tid)
        .order_by(AuditLog.seq.desc())
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

    Serialises writers per tenant with ``pg_advisory_xact_lock`` (released at
    transaction end), computes ``prev_hash`` and ``event_hash`` and flushes
    the row so ``seq`` is assigned. The caller commits the transaction.
    """
    tid = _coerce_uuid(tenant_id)
    sid: uuid.UUID | None = _coerce_uuid(session_id) if session_id is not None else None

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"audit:{tid}"},
    )
    prev_hash = await get_last_hash(db, tid)
    event_hash = compute_hash(
        payload, prev_hash, event_type=event_type, session_id=sid, actor_sub=actor_sub
    )

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
    await db.flush()
    audit_events_total.inc(tenant_id=str(tid), event_type=event_type)
    return entry


async def verify_chain(
    db: AsyncSession, tenant_id: uuid.UUID | str
) -> dict[str, Any]:
    """Verify the full hash chain for *tenant_id*.

    Returns a dict::

        {
            "ok": bool,
            "total": int,
            "broken": [{"index": int, "id": str, "reason": "hash|link",
                        "expected": str, "got": str}],
        }

    ``reason="hash"``: the stored ``event_hash`` does not match a recomputation
    over the row (payload/metadata edited).
    ``reason="link"``: the row's ``prev_hash`` does not equal the previous
    row's ``event_hash`` (row deleted/inserted — gap in the chain).
    """
    tid = _coerce_uuid(tenant_id)
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tid)
        .order_by(AuditLog.seq.asc())
    )
    result = await db.execute(stmt)
    rows: list[AuditLog] = list(result.scalars().all())

    broken: list[dict[str, Any]] = []
    expected_prev = GENESIS_HASH
    for i, row in enumerate(rows):
        if row.prev_hash != expected_prev:
            broken.append({
                "index": i,
                "id": str(row.id),
                "reason": "link",
                "expected": expected_prev,
                "got": row.prev_hash,
            })
        expected = compute_hash(
            row.payload,
            row.prev_hash,
            event_type=row.event_type,
            session_id=row.session_id,
            actor_sub=row.actor_sub,
        )
        if expected != row.event_hash:
            broken.append({
                "index": i,
                "id": str(row.id),
                "reason": "hash",
                "expected": expected,
                "got": row.event_hash,
            })
        expected_prev = row.event_hash

    return {"ok": len(broken) == 0, "total": len(rows), "broken": broken}
