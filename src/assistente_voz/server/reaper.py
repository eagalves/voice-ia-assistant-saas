"""Auto-finalize reaper — Issue #16 (PRD: hard cap 4h / auto-finalize 6h).

``sessions.auto_finalize_at`` was only *recorded* until now; nothing closed
a Sessão whose app crashed without calling ``/close``. The reaper runs in
every container (lifespan task) and, every ``SESSION_REAPER_INTERVAL_S``:

1. lists open Sessões past ``auto_finalize_at`` (cross-tenant system read);
2. re-reads each one ``FOR UPDATE`` inside its own tenant-scoped
   repository and, if still open, applies the shared close transaction
   (``session_lifecycle.close_session_tx`` → token revoked, stream TTL,
   draft ``pending``, AuditLog ``session.auto_finalized``);
3. schedules the draft materialization worker after commit.

Multiple containers are safe: the row lock + status re-check make the
second reaper a no-op for the same Sessão.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .db.models import Session
from .db.session import get_sessionmaker
from .event_stream import EventStreamClient
from .repositories.sessions_repo import SessionsRepository, SystemSessionsRepository
from .session_lifecycle import (
    REASON_AUTO_FINALIZE,
    close_session_tx,
    materialization_worker_for,
)
from .stream_client import get_stream_client

logger = logging.getLogger(__name__)

REAPER_ACTOR = "system:reaper"


class SessionReaper:
    def __init__(
        self,
        interval_s: float,
        *,
        stream_client: EventStreamClient | None = None,
        batch_limit: int = 100,
    ) -> None:
        self._interval_s = float(interval_s)
        self._stream = stream_client
        self._batch_limit = batch_limit
        self._tasks: set[asyncio.Task] = set()
        self.reaped_total: int = 0

    async def reap_once(self, *, now: datetime | None = None) -> int:
        """Close every expired open Sessão once; return how many were closed."""
        stream = self._stream or get_stream_client()
        closed: list[tuple[str, Session]] = []

        async with get_sessionmaker()() as db:
            async with db.begin():
                expired = await SystemSessionsRepository(db).list_expired_open(
                    now=now, limit=self._batch_limit
                )
                for row in expired:
                    tenant_id = str(row.tenant_id)
                    repo = SessionsRepository(db, tenant_id)
                    session = await repo.get_for_update(row.id)
                    if session is None or session.status != "open":
                        continue
                    await close_session_tx(
                        db,
                        tenant_id=tenant_id,
                        session=session,
                        stream=stream,
                        reason=REASON_AUTO_FINALIZE,
                        actor_sub=REAPER_ACTOR,
                    )
                    closed.append((tenant_id, session))

        for tenant_id, session in closed:
            logger.warning(
                "Session auto-finalized: tenant=%s session=%s auto_finalize_at=%s",
                tenant_id,
                session.id,
                session.auto_finalize_at.isoformat(),
            )
            worker = materialization_worker_for(
                session, tenant_id=tenant_id, actor_sub=REAPER_ACTOR, stream=stream
            )
            task = asyncio.create_task(worker.run())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self.reaped_total += len(closed)
        return len(closed)

    async def run_forever(self) -> None:
        """Sleep first (startup must not race the test fixtures), then loop."""
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let the reaper die
                logger.exception("Session reaper iteration failed")
