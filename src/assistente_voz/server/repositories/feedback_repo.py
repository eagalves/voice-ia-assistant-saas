from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from ..db.models import DraftFeedback
from .base import TenantRepository


class FeedbackRepository(TenantRepository):
    async def insert(
        self,
        *,
        session_id: uuid.UUID,
        chars_original: int,
        chars_edited: int,
        correction_ratio: float,
        fields_corrected: list[str],
        session_kind: str,
        actor_sub: str | None,
    ) -> DraftFeedback:
        row = DraftFeedback(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            session_id=session_id,
            chars_original=chars_original,
            chars_edited=chars_edited,
            correction_ratio=Decimal(str(round(correction_ratio, 4))),
            fields_corrected=fields_corrected,
            session_kind=session_kind,
            actor_sub=actor_sub,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def list_by_session(self, session_id: uuid.UUID) -> list[DraftFeedback]:
        stmt = self._scope_tenant(
            select(DraftFeedback).where(DraftFeedback.session_id == session_id),
            DraftFeedback,
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
