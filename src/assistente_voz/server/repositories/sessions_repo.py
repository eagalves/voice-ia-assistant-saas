from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Session

AUTO_FINALIZE_AFTER = timedelta(hours=6)


class SessionsRepository:
    """Tenant-aware CRUD for `sessions`.

    Issue #10 will replace direct ORM access with a `TenantRepository`
    base that enforces `WHERE tenant_id = :tid` by construction. For now
    the tenant_id is passed explicitly on every call to lock in the
    pattern.
    """

    def __init__(self, db: AsyncSession, tenant_id: str):
        self.db = db
        self.tenant_id = uuid.UUID(tenant_id)

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
        stmt = select(Session).where(
            Session.id == session_id,
            Session.tenant_id == self.tenant_id,
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
