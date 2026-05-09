from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    """Per-request context that flows through every resolver.

    Issue #1 fixes `tenant_id` to the default UUID. Issue #6 will populate
    `actor_sub` from the JWT and Issue #10 will enforce its propagation
    through `TenantRepository`.
    """

    tenant_id: str
    session_id: str
    session_kind: str
    seniority: str | None
    actor_role: str
    plan: str
