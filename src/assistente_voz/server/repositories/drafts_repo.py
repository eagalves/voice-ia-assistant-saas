from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Draft


class DraftsRepository:
    def __init__(self, db: AsyncSession, tenant_id: str):
        self.db = db
        self.tenant_id = uuid.UUID(tenant_id)

    async def get_by_session(self, session_id: uuid.UUID) -> Draft | None:
        stmt = select(Draft).where(
            Draft.session_id == session_id,
            Draft.tenant_id == self.tenant_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_pending(self, session_id: uuid.UUID) -> Draft:
        draft = Draft(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            session_id=session_id,
            draft_status="pending",
            partial=False,
            retry_count=0,
        )
        self.db.add(draft)
        await self.db.flush()
        return draft

    async def reset_to_pending(self, draft: Draft) -> Draft:
        draft.draft_status = "pending"
        draft.last_error = None
        draft.retry_count = draft.retry_count + 1
        draft.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return draft

    async def force_status(
        self,
        draft: Draft,
        *,
        status: str,
        content: dict[str, Any] | None = None,
        partial: bool = False,
        last_error: str | None = None,
    ) -> Draft:
        """Test/seed-only helper to drive deterministic state in fixtures."""
        draft.draft_status = status
        draft.content = content
        draft.partial = partial
        draft.last_error = last_error
        draft.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return draft
