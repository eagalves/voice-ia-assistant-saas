from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

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

    async def mark_closed(self, session: Session) -> Session:
        session.status = "closed"
        session.closed_at = datetime.now(timezone.utc)
        await self.db.flush()
        return session

    async def rotate_ws_token(self, session: Session, new_token: uuid.UUID) -> Session:
        session.ws_token = str(new_token)
        await self.db.flush()
        return session
