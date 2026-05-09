"""Idempotency keys table.

Revision ID: 20260509_0003
Revises: 20260509_0002
Create Date: 2026-05-09

Issue #2. First-write-wins idempotency for POST /v1/sessions scoped by
(tenant_id, endpoint, actor_sub_jwt, idempotency_key). TTL 24h.

The UNIQUE constraint is the conflict target used by INSERT … ON CONFLICT DO
NOTHING in IdempotencyRepository.store(). The index on expires_at supports
the nightly cleanup job (DELETE WHERE expires_at < now()).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260509_0003"
down_revision: Union[str, None] = "20260509_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_TENANT_ID}'::uuid"),
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("actor_sub_jwt", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("response_body", postgresql.JSONB, nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "endpoint",
            "actor_sub_jwt",
            "idempotency_key",
            name="uq_idempotency_keys_scope",
        ),
    )
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
