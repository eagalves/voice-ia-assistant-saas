"""Tenant-scoped Redis key builders.

Issue #10. Every Redis key that touches per-tenant data MUST include
``{tenant_id}`` as the Redis hash tag — that is the contract the future
Cluster (Issue #3 / Fase 3) relies on for slot affinity. Bypassing this
helper is forbidden; the test
``tests/fase2/test_issue010_tenant_isolation.py::test_redis_key_format``
scans the source tree for any ``redis.*`` call whose key argument is
constructed without going through one of these functions.

The hash tag braces are part of the literal key string — Redis keeps
them; do not strip.
"""

from __future__ import annotations

import uuid

PREFIX_SESSION_EVENTS = "events"
PREFIX_SESSION_STATE = "state_snapshot"
PREFIX_WS_TOKEN = "ws:token"
PREFIX_WS_REVOCATIONS = "ws:revocations"


def _normalize_tenant(tenant_id: str | uuid.UUID) -> str:
    if isinstance(tenant_id, uuid.UUID):
        return str(tenant_id)
    # Validate it parses, but keep the original casing/hyphenation.
    uuid.UUID(tenant_id)
    return tenant_id


def _normalize_session(session_id: str | uuid.UUID) -> str:
    if isinstance(session_id, uuid.UUID):
        return str(session_id)
    uuid.UUID(session_id)
    return session_id


def session_events_key(
    tenant_id: str | uuid.UUID, session_id: str | uuid.UUID
) -> str:
    """Stream of transcription events for a Sessão.

    Format: ``session:{<tenant_id>:<session_id>}:events`` — Redis hash
    tag is the inner ``{...}`` so the whole Sessão lives on one slot.
    """
    tid = _normalize_tenant(tenant_id)
    sid = _normalize_session(session_id)
    return f"session:{{{tid}:{sid}}}:{PREFIX_SESSION_EVENTS}"


def session_state_key(
    tenant_id: str | uuid.UUID, session_id: str | uuid.UUID
) -> str:
    tid = _normalize_tenant(tenant_id)
    sid = _normalize_session(session_id)
    return f"session:{{{tid}:{sid}}}:{PREFIX_SESSION_STATE}"


def ws_token_key(
    tenant_id: str | uuid.UUID, ws_token: str | uuid.UUID
) -> str:
    """Lookup key for a ``ws_token``.

    The hash tag is the ``tenant_id`` alone — ws_token revocation channel
    is tenant-scoped, not session-scoped, so the key colocates with the
    revocations channel.
    """
    tid = _normalize_tenant(tenant_id)
    token = ws_token if isinstance(ws_token, str) else str(ws_token)
    return f"{PREFIX_WS_TOKEN}:{{{tid}}}:{token}"


def ws_revocations_channel(tenant_id: str | uuid.UUID) -> str:
    tid = _normalize_tenant(tenant_id)
    return f"{PREFIX_WS_REVOCATIONS}:{{{tid}}}"


def extract_hash_tag(key: str) -> str | None:
    """Return the hash tag content (without braces) or ``None`` if absent."""
    open_idx = key.find("{")
    close_idx = key.find("}", open_idx + 1) if open_idx >= 0 else -1
    if open_idx < 0 or close_idx <= open_idx + 1:
        return None
    return key[open_idx + 1 : close_idx]
