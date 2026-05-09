"""MaterializationWorker — Issue #5.

Transitions a draft from ``pending`` → ``ready`` with Postgres advisory-lock
based exactly-once semantics and automatic retry (3 attempts, exponential
backoff).

Usage (background task)::

    worker = MaterializationWorker(
        tenant_id=ctx.tenant_id,
        session_id=ctx.session_id,
        stream_client=fake_or_real_redis,
    )
    background_tasks.add_task(worker.run)

The worker creates its own ``AsyncSession`` via ``get_sessionmaker()`` so it
does not hold the HTTP request session open.

``content_builder`` is injectable for tests.  The default stub aggregates the
Redis Stream events into ``{"events": [...], "event_count": N}``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .db.session import get_sessionmaker
from .event_stream import EventStreamClient, FakeEventStream
from .field_confidence import enrich_draft_content, get_field_confidence_config
from .redis_keys import session_events_key
from .repositories.drafts_repo import DraftsRepository

logger = logging.getLogger(__name__)

# Retry policy
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0  # attempt n waits BACKOFF_BASE_S * 2**(n-1) seconds

ContentBuilder = Callable[[str, str, EventStreamClient], Awaitable[dict]]


async def _default_content_builder(
    tenant_id: str,
    session_id: str,
    stream_client: EventStreamClient,
) -> dict:
    """Aggregate all Redis Stream events into a minimal draft content dict.

    Production code will replace this with the full LLM-based summarisation
    pipeline.  For now it serves as a deterministic, self-contained stub
    that makes the exactly-once and retry tests work without mocking LLM.
    """
    key = session_events_key(tenant_id, session_id)
    entries = await stream_client.xrange(key)
    result = {
        "events": [fields for _, fields in entries],
        "event_count": len(entries),
        "materialized_at": datetime.now(timezone.utc).isoformat(),
    }
    return enrich_draft_content(result, get_field_confidence_config())


class MaterializationWorker:
    """Runs the draft materialization pipeline with retry and advisory lock."""

    def __init__(
        self,
        *,
        tenant_id: str,
        session_id: str,
        stream_client: EventStreamClient | None = None,
        content_builder: ContentBuilder | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id
        self._stream_client: EventStreamClient = stream_client or FakeEventStream()
        self._content_builder: ContentBuilder = (
            content_builder or _default_content_builder
        )

    async def run(self) -> None:
        """Execute materialization with up to MAX_ATTEMPTS retries.

        On total failure marks the draft as ``failed`` and logs at ERROR.
        Exits silently if the draft is already ``ready`` (concurrent close +
        auto-finalize race).
        """
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
            try:
                ok = await self._try_once()
                if ok is None:
                    # Draft was not in pending state — exit silently.
                    return
                if ok:
                    logger.info(
                        "Draft materialized: tenant=%s session=%s attempt=%d",
                        self.tenant_id,
                        self.session_id,
                        attempt + 1,
                    )
                    return
                # ok=False means advisory lock was acquired but status != pending
                # (another worker already finished) — exit silently.
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Materialization attempt %d/%d failed: tenant=%s session=%s error=%s",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    self.tenant_id,
                    self.session_id,
                    exc,
                )

        # All attempts exhausted — mark failed.
        await self._mark_failed(last_exc)

    async def _try_once(self) -> bool | None:
        """One materialization attempt inside its own DB transaction.

        Returns:
            True  — successfully transitioned to ready.
            False — draft not pending; exit silently.
            None  — draft not found; exit silently.
        """
        content = await self._content_builder(
            self.tenant_id, self.session_id, self._stream_client
        )

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as db:
            async with db.begin():
                import uuid as _uuid

                repo = DraftsRepository(db, self.tenant_id)
                result = await repo.materialize_with_lock(
                    _uuid.UUID(self.session_id), content
                )
                return result

    async def _mark_failed(self, exc: Exception | None) -> None:
        """Mark draft as failed after all retries are exhausted."""
        import uuid as _uuid

        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as db:
                async with db.begin():
                    repo = DraftsRepository(db, self.tenant_id)
                    draft = await repo.get_by_session(_uuid.UUID(self.session_id))
                    if draft is not None and draft.draft_status != "ready":
                        await repo.force_status(
                            draft,
                            status="failed",
                            partial=True,
                            last_error=str(exc) if exc else "Unknown error",
                        )
        except Exception as mark_exc:  # noqa: BLE001
            logger.error(
                "Failed to mark draft as failed: tenant=%s session=%s error=%s",
                self.tenant_id,
                self.session_id,
                mark_exc,
            )

        logger.error(
            "Draft materialization FAILED after %d attempts: tenant=%s session=%s error=%s",
            MAX_ATTEMPTS,
            self.tenant_id,
            self.session_id,
            exc,
        )
