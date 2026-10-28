"""AuditLog integrity hardening + draft_feedback ratio type.

Revision ID: 20261027_0006
Revises: 20260509_0005
Create Date: 2026-10-27

ADR 0004 / Issue #12 follow-up:

* ``audit_log.seq`` — server-assigned identity column that defines the hash
  chain order. ``created_at`` is Postgres' transaction start time, so two
  events in one transaction collide and the chain could not be verified
  deterministically.
* Append-only trigger — ``UPDATE`` / ``DELETE`` on ``audit_log`` raise.
  Defence-in-depth for the DPIA residual risk: a DBA must *disable the
  trigger* (a privileged, CloudTrail-visible action) before rewriting.
  ``TRUNCATE`` is not blocked (used by the test fixtures).
* Drop the ``audit_log.session_id`` foreign key — audit events outlive
  sessions (5 years vs 6–12 months) and ``ON DELETE SET NULL`` would need an
  UPDATE that the trigger forbids.
* ``draft_feedback.correction_ratio`` becomes ``double precision`` (spec:
  FLOAT). ``NUMERIC(5,4)`` overflowed when ``chars_edited`` exceeded
  ``chars_original`` by more than 10x.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261027_0006"
down_revision: Union[str, None] = "20260509_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRIGGER_FN = "audit_log_block_mutation"
TRIGGER = "trg_audit_log_append_only"


def upgrade() -> None:
    # 1. seq identity column + unique index (chain order).
    op.add_column(
        "audit_log",
        sa.Column(
            "seq",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_log_seq", "audit_log", ["seq"], unique=True)

    # 2. session_id is no longer a FK (audit outlives sessions).
    op.drop_constraint("audit_log_session_id_fkey", "audit_log", type_="foreignkey")

    # 3. Append-only trigger.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {TRIGGER_FN}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% on seq=%)', TG_OP, OLD.seq
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER}
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FN}();
        """
    )

    # 4. correction_ratio → double precision.
    op.alter_column(
        "draft_feedback",
        "correction_ratio",
        type_=sa.Float(precision=53),
        existing_type=sa.Numeric(5, 4),
        existing_nullable=False,
        postgresql_using="correction_ratio::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "draft_feedback",
        "correction_ratio",
        type_=sa.Numeric(5, 4),
        existing_type=sa.Float(precision=53),
        existing_nullable=False,
        postgresql_using="LEAST(correction_ratio, 9.9999)::numeric(5,4)",
    )
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON audit_log;")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FN}();")
    op.create_foreign_key(
        "audit_log_session_id_fkey",
        "audit_log",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("ix_audit_log_seq", table_name="audit_log")
    op.drop_column("audit_log", "seq")
