"""Global EventStreamClient singleton — Issue #6.

In production this wires ``redis.asyncio``.  In tests ``override_stream_client()``
injects a ``FakeEventStream`` (analogous to ``override_engine()`` in db/session.py).

When ``jwt_jwks_url`` is empty (bypass mode / tests that don't set env vars)
the default is a fresh ``FakeEventStream``.
"""
from __future__ import annotations

from .event_stream import EventStreamClient, FakeEventStream

_client: EventStreamClient | None = None


def get_stream_client() -> EventStreamClient:
    """Return the global EventStreamClient, initialising if needed."""
    global _client
    if _client is None:
        # Default to FakeEventStream (safe for tests; real Redis injected in prod)
        _client = FakeEventStream()
    return _client


def override_stream_client(client: EventStreamClient) -> None:
    """Test hook — replace the global client with *client*."""
    global _client
    _client = client


def reset_stream_client() -> None:
    """Reset to None (used between test modules if needed)."""
    global _client
    _client = None
