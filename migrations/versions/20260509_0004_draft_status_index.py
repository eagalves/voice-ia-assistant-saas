"""Add index on (tenant_id, draft_status) to drafts table.

Revision ID: 20260509_0004
Revises: 20260509_0003
Create Date: 2026-05-09

Issue #5. Supports efficient tenant-scoped draft status queries used by the
materialisation worker, GET /draft, and the retry endpoint.  Created as a
standard (non-CONCURRENT) index inside Alembic so the migration is
transactional and can be cleanly rolled back.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260509_0004"
down_revision: Union[str, None] = "20260509_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_drafts_tenant_status",
        "drafts",
        ["tenant_id", "draft_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_drafts_tenant_status", table_name="drafts")
