from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=DEFAULT_TENANT_ID,
    )
    session_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    ws_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    auto_finalize_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    draft: Mapped["Draft"] = relationship(
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Issue #2 will promote to a true UNIQUE composite with endpoint+actor_sub.
        # For Issue #1 we keep a non-unique helper index.
        # NOTE: Index defined via Alembic; declared here for documentation only.
    )


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    draft_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    partial: Mapped[bool] = mapped_column(
        nullable=False, server_default="false"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[Session] = relationship(back_populates="draft")

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_drafts_session_id"),
        CheckConstraint(
            "draft_status IN ('pending','ready','failed')",
            name="ck_drafts_status_enum",
        ),
    )


class DraftFeedback(Base):
    __tablename__ = "draft_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    chars_original: Mapped[int] = mapped_column(Integer, nullable=False)
    chars_edited: Mapped[int] = mapped_column(Integer, nullable=False)
    # FLOAT (double precision) per ISSUES_FASE2 #9 — the previous NUMERIC(5,4)
    # overflowed (500 error) when the physician added more text than the draft
    # had (ratio > 9.9999). Migration 0006 widened the column.
    correction_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    fields_corrected: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    session_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyKey(Base):
    """Idempotency record for Issue #2.

    Uniqueness scope: (tenant_id, endpoint, actor_sub_jwt, idempotency_key).
    TTL enforced via expires_at; expired rows are purged by the repository
    before inserting a fresh record (so the UNIQUE constraint doesn't block
    logically-new requests).
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    actor_sub_jwt: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "endpoint",
            "actor_sub_jwt",
            "idempotency_key",
            name="uq_idempotency_keys_scope",
        ),
    )


class AuditLog(Base):
    """Append-only audit log with SHA-256 hash chain.

    Chain order is the server-assigned ``seq`` (identity column), never
    ``created_at`` — ``now()`` is the *transaction* start time in Postgres,
    so two events written in one transaction share a timestamp.

    ``event_hash`` covers the whole record (``event_type``, ``session_id``,
    ``actor_sub`` and ``payload``) plus ``prev_hash``; see ``audit_log.py``.
    The first event in a tenant's chain uses ``GENESIS_HASH`` ("0" * 64).

    ``session_id`` is intentionally *not* a foreign key: audit events are
    retained for 5 years, sessions for 6–12 months, and an ``ON DELETE SET
    NULL`` would require an UPDATE on a table protected by the append-only
    trigger (migration 0006).

    Issue #12 — HITL Compliance; ADR 0004.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    actor_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
