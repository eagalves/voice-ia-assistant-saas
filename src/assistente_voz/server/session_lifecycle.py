"""Shared Sessão close / auto-finalize transaction — Issues #5, #6, #12, #16.

Both ``POST /v1/sessions/{id}/close`` and the auto-finalize reaper must
perform exactly the same state transition; keeping it in one place is what
guarantees the AuditLog, the Redis TTL and the token revocation never drift
between the two paths.

Transaction contents (caller owns commit/rollback):

1. ``sessions.status = closed`` (+ ``closed_at``)
2. ``ws_token`` deleted from Redis and ``revoke:{token}`` published
   (revocation of an established WS in < 2s — Issue #6)
3. ``EXPIRE`` of the Sessão's Redis Stream (24h — Issue #5)
4. ``drafts`` row inserted as ``pending`` (idempotent)
5. AuditLog event ``session.closed`` / ``session.auto_finalized``

Materialization itself is scheduled by the caller *after* commit, via
``materialization_worker_for``.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .audio_gateway import STREAM_TTL_SECONDS
from .audit_log import EVENT_SESSION_AUTO_FINALIZED, EVENT_SESSION_CLOSED, log_event
from .db.models import Draft, Session
from .event_stream import EventStreamClient
from .materialization import MaterializationWorker
from .redis_keys import session_events_key, ws_revocations_channel, ws_token_key
from .repositories.drafts_repo import DraftsRepository
from .repositories.sessions_repo import SessionsRepository

REASON_CLIENT_CLOSE = "client_close"
REASON_AUTO_FINALIZE = "auto_finalize"


async def close_session_tx(
    db: AsyncSession,
    *,
    tenant_id: str,
    session: Session,
    stream: EventStreamClient,
    reason: str = REASON_CLIENT_CLOSE,
    actor_sub: str | None = None,
) -> Draft:
    """Close *session* inside the caller's transaction and return its draft row."""
    sessions = SessionsRepository(db, tenant_id)
    drafts = DraftsRepository(db, tenant_id)

    await sessions.mark_closed(session)

    old_token = session.ws_token
    await stream.delete(ws_token_key(tenant_id, old_token))
    await stream.publish(ws_revocations_channel(tenant_id), f"revoke:{old_token}")
    await stream.expire(session_events_key(tenant_id, str(session.id)), STREAM_TTL_SECONDS)

    draft = await drafts.insert_pending(session.id)

    event_type = (
        EVENT_SESSION_AUTO_FINALIZED if reason == REASON_AUTO_FINALIZE else EVENT_SESSION_CLOSED
    )
    assert session.closed_at is not None
    await log_event(
        db,
        tenant_id=tenant_id,
        event_type=event_type,
        session_id=session.id,
        actor_sub=actor_sub,
        payload={
            "reason": reason,
            "session_kind": session.session_kind,
            "plan": session.plan,
            "closed_at": session.closed_at.isoformat(),
        },
    )
    return draft


def materialization_worker_for(
    session: Session,
    *,
    tenant_id: str,
    actor_sub: str | None = None,
    stream: EventStreamClient | None = None,
) -> MaterializationWorker:
    """Build the worker that turns the closed Sessão's stream into a draft."""
    return MaterializationWorker(
        tenant_id=tenant_id,
        session_id=str(session.id),
        session_kind=session.session_kind,
        actor_sub=actor_sub,
        stream_client=stream,
    )
