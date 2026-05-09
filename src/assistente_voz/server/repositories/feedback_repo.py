from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import DraftFeedback


class FeedbackRepository:
    def __init__(self, db: AsyncSession, tenant_id: str):
        self.db = db
        self.tenant_id = uuid.UUID(tenant_id)

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
