"""Tenant isolation: RLS policies (written, NOT forced).

Revision ID: 20260509_0002
Revises: 20260509_0001
Create Date: 2026-05-09

Issue #10. The policies are defined and RLS is enabled on every
tenant-bearing table, but `FORCE ROW LEVEL SECURITY` is **not** issued.
That means the table owner (the migration role, typically the same role
the application uses in Fase 2.0/2.1) bypasses the policies — RLS is
"written but off in production" as required by the PRD.

Staging shadow mode flips this with::

    ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
    -- (and per-table for drafts, draft_feedback)

The application is expected to set ``app.tenant_id`` per request once
this is forced (see ``server/db/session.py`` for the hook). Until then
the in-Python ``TenantRepository`` is the only enforcement; RLS is the
defence-in-depth backstop.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260509_0002"
down_revision: Union[str, None] = "20260509_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("sessions", "drafts", "draft_feedback")
POLICY = "tenant_isolation"


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {POLICY} ON {table}
                USING (
                    tenant_id::text =
                        current_setting('app.tenant_id', true)
                )
                WITH CHECK (
                    tenant_id::text =
                        current_setting('app.tenant_id', true)
                );
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
