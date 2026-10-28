from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db.models import Draft
from .base import TenantRepository


class DraftsRepository(TenantRepository):
    async def get_by_session(self, session_id: uuid.UUID) -> Draft | None:
        stmt = self._scope_tenant(
            select(Draft).where(Draft.session_id == session_id), Draft
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_pending(self, session_id: uuid.UUID) -> Draft:
        """Insert the ``pending`` draft row for a Sessão (idempotent).

        ``INSERT … ON CONFLICT (session_id) DO NOTHING`` so a concurrent
        ``close`` + auto-finalize race never surfaces a unique violation;
        the canonical row (ours or the winner's) is returned.
        """
        stmt = (
            pg_insert(Draft)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                session_id=session_id,
                draft_status="pending",
                partial=False,
                retry_count=0,
            )
            .on_conflict_do_nothing(constraint="uq_drafts_session_id")
        )
        await self.db.execute(stmt)
        await self.db.flush()
        draft = await self.get_by_session(session_id)
        assert draft is not None  # row exists after insert-or-conflict
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

    async def materialize_with_lock(
        self,
        session_id: uuid.UUID,
        content: dict[str, Any],
    ) -> bool | None:
        """Acquire advisory lock and transition draft pending → ready (exactly-once).

        Uses ``pg_advisory_xact_lock`` so that concurrent materialization
        workers (close + auto-finalize, retry race) queue on the lock and the
        second one exits silently when it sees the draft is already ``ready``.

        Returns:
            True   — draft transitioned to ``ready``.
            False  — draft was not in ``pending`` state; exited silently.
            None   — no draft row exists for the session.
        """
        lock_key = f"materialize:{self.tenant_id}:{session_id}"
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": lock_key},
        )

        draft = await self.get_by_session(session_id)
        if draft is None:
            return None
        if draft.draft_status != "pending":
            return False

        draft.draft_status = "ready"
        draft.content = content
        draft.partial = False
        draft.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True
