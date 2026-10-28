from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Session
from .base import TenantRepository

AUTO_FINALIZE_AFTER = timedelta(hours=6)


class SessionsRepository(TenantRepository):
    """Tenant-aware CRUD for `sessions`."""

    async def create(
        self,
        *,
        session_kind: str,
        actor_role: str,
        plan: str,
        seniority: str | None,
        plan_features: dict[str, Any],
        idempotency_key: str,
        ws_token: uuid.UUID,
    ) -> Session:
        now = datetime.now(timezone.utc)
        session = Session(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            session_kind=session_kind,
            actor_role=actor_role,
            plan=plan,
            seniority=seniority,
            plan_features=plan_features,
            idempotency_key=idempotency_key,
            ws_token=str(ws_token),
            status="open",
            created_at=now,
            auto_finalize_at=now + AUTO_FINALIZE_AFTER,
        )
        self.db.add(session)
        await self.db.flush()
        return session

    async def get(self, session_id: uuid.UUID) -> Session | None:
        stmt = self._scope_tenant(
            select(Session).where(Session.id == session_id), Session
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_update(self, session_id: uuid.UUID) -> Session | None:
        """Row-locked read (``SELECT ... FOR UPDATE``) for state transitions.

        Serialises concurrent ``close`` / ``auto-finalize`` / ``token/refresh``
        on the same Sessão so the second writer sees the first one's state.
        """
        stmt = self._scope_tenant(
            select(Session).where(Session.id == session_id).with_for_update(),
            Session,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_closed(self, session: Session) -> Session:
        session.status = "closed"
        session.closed_at = datetime.now(timezone.utc)
        await self.db.flush()
        return session

    async def rotate_ws_token(self, session: Session, new_token: uuid.UUID) -> Session:
        session.ws_token = str(new_token)
        await self.db.flush()
        return session


class SystemSessionsRepository:
    """Cross-tenant, system-job-only access to ``sessions``.

    The auto-finalize reaper (Issue #16) must find expired open sessions of
    *every* tenant; it is the single sanctioned cross-tenant reader. It never
    mutates rows — mutations go through a tenant-scoped ``SessionsRepository``
    built from the row's own ``tenant_id``.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_expired_open(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[Session]:
        now = now or datetime.now(timezone.utc)
        stmt = (
            select(Session)
            .where(Session.status == "open")
            .where(Session.auto_finalize_at <= now)
            .order_by(Session.auto_finalize_at.asc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
