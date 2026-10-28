"""Idempotency-key repository — Issue #2.

First-write-wins semantics scoped by
(tenant_id, endpoint, actor_sub_jwt, idempotency_key).

Invariants:
- Expired records are purged for the exact key combination before each
  INSERT so the UNIQUE constraint never blocks a logically-new request.
- INSERT uses ON CONFLICT DO NOTHING; a follow-up SELECT fetches the
  canonical record (either freshly inserted or the winner of a race).
  Callers compare the returned record with what they tried to store to
  detect a lost race (see ``routers/sessions.py``).
- ``canonical_hash`` is a module-level helper exported for use in tests
  and the router; it is the only authoritative definition of
  SHA-256(json.dumps(sort_keys=True, ensure_ascii=False)).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import IdempotencyKey
from .base import TenantRepository

TTL_HOURS = 24
ENDPOINT_CREATE_SESSION = "POST /v1/sessions"


def canonical_hash(body: Any) -> str:
    """SHA-256 of the canonical JSON serialisation.

    Deterministic regardless of key insertion order because sort_keys=True.
    ensure_ascii=False preserves pt-BR characters without percent-encoding.
    """
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyRepository(TenantRepository):
    """Repository for idempotency_keys — inherits TenantRepository contract."""

    def __init__(self, db: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        super().__init__(db, tenant_id)

    async def get_valid(
        self,
        endpoint: str,
        actor_sub_jwt: str,
        idempotency_key: str,
    ) -> IdempotencyKey | None:
        """Return the existing non-expired record, or None."""
        now = datetime.now(timezone.utc)
        stmt = self._scope_tenant(
            select(IdempotencyKey)
            .where(IdempotencyKey.endpoint == endpoint)
            .where(IdempotencyKey.actor_sub_jwt == actor_sub_jwt)
            .where(IdempotencyKey.idempotency_key == idempotency_key)
            .where(IdempotencyKey.expires_at > now),
            IdempotencyKey,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def store(
        self,
        endpoint: str,
        actor_sub_jwt: str,
        idempotency_key: str,
        payload_hash: str,
        response_body: dict[str, Any],
        response_status: int,
    ) -> IdempotencyKey | None:
        """Persist the response.

        Steps:
        1. Purge any expired row for this exact (tenant, endpoint, actor, key)
           so the UNIQUE constraint doesn't block a logically-new request.
        2. INSERT … ON CONFLICT DO NOTHING (handles concurrent first-writes:
           the second writer blocks on the unique index until the first
           commits, then its INSERT is a no-op).
        3. Flush and SELECT to get the canonical record.

        Returns None only in the pathological case where a concurrent writer
        holds the lock and the row is still not visible — callers should treat
        None as "proceed without idempotency store" (best-effort).
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=TTL_HOURS)

        # Step 1: purge expired record for this exact combination.
        del_stmt = self._scope_tenant(
            delete(IdempotencyKey)
            .where(IdempotencyKey.endpoint == endpoint)
            .where(IdempotencyKey.actor_sub_jwt == actor_sub_jwt)
            .where(IdempotencyKey.idempotency_key == idempotency_key)
            .where(IdempotencyKey.expires_at <= now),
            IdempotencyKey,
        )
        await self.db.execute(del_stmt)

        # Step 2: INSERT, ignore conflict.
        ins_stmt = (
            pg_insert(IdempotencyKey)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                endpoint=endpoint,
                actor_sub_jwt=actor_sub_jwt,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                response_body=response_body,
                response_status=response_status,
                created_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_scope")
        )
        await self.db.execute(ins_stmt)
        await self.db.flush()

        # Step 3: fetch canonical record (ours or the race-winner's).
        return await self.get_valid(endpoint, actor_sub_jwt, idempotency_key)
