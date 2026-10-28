"""Issue #16 — Auto-finalize: reaper fecha Sessões além de ``auto_finalize_at``.

AC1 — Sessão aberta com ``auto_finalize_at`` no passado é fechada pelo ``reap_once``
       (status ``closed``, draft ``pending``→``ready``, token revogado, TTL do stream)
AC2 — AuditLog registra ``session.auto_finalized`` com ``actor_sub=system:reaper``
AC3 — Sessão ainda dentro do prazo não é tocada
AC4 — Segunda execução é no-op (idempotente)
AC5 — ``SESSION_REAPER_INTERVAL_S=0`` desliga o reaper no lifespan
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.audio_gateway import STREAM_TTL_SECONDS
from assistente_voz.server.audit_log import EVENT_SESSION_AUTO_FINALIZED, verify_chain
from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db.models import AuditLog, Draft, Session
from assistente_voz.server.event_stream import FakeEventStream
from assistente_voz.server.reaper import REAPER_ACTOR, SessionReaper
from assistente_voz.server.redis_keys import session_events_key, ws_revocations_channel, ws_token_key
from assistente_voz.server.repositories.sessions_repo import SessionsRepository


async def _create_open_session(db_engine: AsyncEngine, *, expired: bool) -> Session:
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            session = await SessionsRepository(db, DEFAULT_TENANT_ID).create(
                session_kind="medical", actor_role="doctor", plan="basico", seniority=None,
                plan_features={"session_max_minutes": 60}, idempotency_key=str(uuid.uuid4()),
                ws_token=uuid.uuid4(),
            )
            if expired:
                session.auto_finalize_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    return session


async def test_ac1_ac2_expired_session_is_auto_finalized(db_engine: AsyncEngine, truncate_tables: None):
    stream = FakeEventStream()
    session = await _create_open_session(db_engine, expired=True)
    token_key = ws_token_key(DEFAULT_TENANT_ID, session.ws_token)
    await stream.set(token_key, "{}", ex=3600)

    reaper = SessionReaper(interval_s=0, stream_client=stream)
    closed = await reaper.reap_once()
    assert closed == 1
    await asyncio.gather(*list(reaper._tasks), return_exceptions=True)  # noqa: SLF001 — wait materialization

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (await db.execute(select(Session).where(Session.id == session.id))).scalar_one()
        draft = (await db.execute(select(Draft).where(Draft.session_id == session.id))).scalar_one()
        events = (
            await db.execute(select(AuditLog).where(AuditLog.session_id == session.id))
        ).scalars().all()
        chain = await verify_chain(db, DEFAULT_TENANT_ID)

    assert row.status == "closed" and row.closed_at is not None
    assert draft.draft_status == "ready"
    assert await stream.get(token_key) is None, "ws_token deve ser revogado"
    assert f"revoke:{session.ws_token}" in stream.published_messages(ws_revocations_channel(DEFAULT_TENANT_ID))
    assert stream.ttl_for(session_events_key(DEFAULT_TENANT_ID, str(session.id))) == STREAM_TTL_SECONDS

    auto = [e for e in events if e.event_type == EVENT_SESSION_AUTO_FINALIZED]
    assert len(auto) == 1 and auto[0].actor_sub == REAPER_ACTOR == "system:reaper"
    assert chain["ok"] is True


async def test_ac3_session_within_deadline_untouched(db_engine: AsyncEngine, truncate_tables: None):
    session = await _create_open_session(db_engine, expired=False)
    reaper = SessionReaper(interval_s=0, stream_client=FakeEventStream())
    assert await reaper.reap_once() == 0
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (await db.execute(select(Session).where(Session.id == session.id))).scalar_one()
    assert row.status == "open"


async def test_ac4_reap_is_idempotent(db_engine: AsyncEngine, truncate_tables: None):
    await _create_open_session(db_engine, expired=True)
    reaper = SessionReaper(interval_s=0, stream_client=FakeEventStream())
    assert await reaper.reap_once() == 1
    assert await reaper.reap_once() == 0
    await asyncio.gather(*list(reaper._tasks), return_exceptions=True)  # noqa: SLF001


def test_ac5_reaper_disabled_with_zero_interval(monkeypatch):
    from assistente_voz.server.config import get_settings, reset_settings_cache

    monkeypatch.setenv("SESSION_REAPER_INTERVAL_S", "0")
    reset_settings_cache()
    try:
        assert get_settings().session_reaper_interval_s == 0
    finally:
        reset_settings_cache()
