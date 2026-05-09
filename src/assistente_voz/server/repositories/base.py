"""Tenant-aware repository base.

Issue #10 establishes the contract: every database access in the server
package goes through a `TenantRepository` subclass. The base class is the
**only** legal place to construct queries that touch tables with a
`tenant_id` column — subclasses use `self._scope_tenant(stmt, Model)` to
inject the mandatory predicate.

CI guardrails (see `tests/fase2/test_issue010_tenant_isolation.py`) lint
the source tree to ensure no other module imports `AsyncSession` /
`Session` directly. The companion RLS migration (0002) writes Postgres
policies as a defence-in-depth: if a future query slips past the lint, RLS
will catch it once enabled in staging shadow mode.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import Delete, Select, Update
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


def _coerce_tenant(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(value)


class TenantRepository:
    """Base class for all server-side repositories.

    The constructor is the canonical place where the tenant scope is
    locked in. Subclasses MUST call `_scope_tenant` on every read/write
    statement — bypass is treated as a bug and caught by the runtime
    cross-tenant test.
    """

    def __init__(self, db: AsyncSession, tenant_id: str | uuid.UUID):
        self.db = db
        self.tenant_id: uuid.UUID = _coerce_tenant(tenant_id)

    def _scope_tenant(self, stmt, model) -> Select | Update | Delete:
        """Inject `WHERE tenant_id = :tid` into the statement.

        Works for `select(...)`, `update(...)` and `delete(...)`. Raises
        `AttributeError` if the model does not declare a `tenant_id`
        column — that's intentional; tables without `tenant_id` should not
        flow through this base class at all.
        """
        column = model.tenant_id  # AttributeError if missing — by design.
        return stmt.where(column == self.tenant_id)
