"""Add audit_log table with hash chain for HITL compliance.

Revision ID: 20260509_0005
Revises: 20260509_0004
Create Date: 2026-05-09

Issue #12. Append-only audit log with SHA-256 hash chain. Tamper detection
relies on ``event_hash = SHA-256(payload_json + prev_hash)``. The genesis
hash for a tenant's first event is ``"0" * 64``.

Indexed on ``(tenant_id, created_at)`` to support chronological chain reads
per tenant without full-table scans.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260509_0005"
down_revision: Union[str, None] = "20260509_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_sub", sa.String(128), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
