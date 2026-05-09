"""Fase 2 initial schema — sessions, drafts, draft_feedback.

Revision ID: 20260509_0001
Revises:
Create Date: 2026-05-09

Issue #1 creates the full table set used by Issues #1, #5 and #9 to avoid
migration churn. The composite UNIQUE on idempotency_keys is intentionally
deferred — Issue #2 adds a dedicated table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260509_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_TENANT_ID}'::uuid"),
        ),
        sa.Column("session_kind", sa.String(64), nullable=False),
        sa.Column("seniority", sa.String(32), nullable=True),
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("plan_features", postgresql.JSONB, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'open'")
        ),
        sa.Column("ws_token", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("auto_finalize_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sessions_tenant_idempotency",
        "sessions",
        ["tenant_id", "idempotency_key"],
        unique=False,
    )

    op.create_table(
        "drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "draft_status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("content", postgresql.JSONB, nullable=True),
        sa.Column(
            "partial",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "retry_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("session_id", name="uq_drafts_session_id"),
        sa.CheckConstraint(
            "draft_status IN ('pending','ready','failed')",
            name="ck_drafts_status_enum",
        ),
    )

    op.create_table(
        "draft_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chars_original", sa.Integer, nullable=False),
        sa.Column("chars_edited", sa.Integer, nullable=False),
        sa.Column("correction_ratio", sa.Numeric(5, 4), nullable=False),
        sa.Column(
            "fields_corrected",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("session_kind", sa.String(64), nullable=False),
        sa.Column("actor_sub", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("draft_feedback")
    op.drop_table("drafts")
    op.drop_index("ix_sessions_tenant_idempotency", table_name="sessions")
    op.drop_table("sessions")
