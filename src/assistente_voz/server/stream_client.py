"""Global EventStreamClient singleton — Issues #6 and #13.

Selection rule (``get_stream_client``):

* ``REDIS_URL`` set   → ``RedisEventStream`` (shared across containers).
* ``REDIS_URL`` empty → ``FakeEventStream`` (in-memory; dev/tests only).

Tests inject a fresh ``FakeEventStream`` via ``override_stream_client()``
(analogous to ``override_engine()`` in ``db/session.py``) and call
``reset_stream_client()`` afterwards.
"""
from __future__ import annotations

import logging

from .config import get_settings
from .event_stream import EventStreamClient, FakeEventStream, RedisEventStream

_logger = logging.getLogger(__name__)

_client: EventStreamClient | None = None


def get_stream_client() -> EventStreamClient:
    """Return the global EventStreamClient, initialising if needed."""
    global _client
    if _client is None:
        url = get_settings().redis_url.strip()
        if url:
            _client = RedisEventStream(url)
            _logger.info("EventStream: using Redis at %s", _redact(url))
        else:
            _client = FakeEventStream()
            _logger.warning(
                "EventStream: REDIS_URL not set — using in-memory FakeEventStream "
                "(no durability, single container only)."
            )
    return _client


def override_stream_client(client: EventStreamClient) -> None:
    """Test hook — replace the global client with *client*."""
    global _client
    _client = client


def reset_stream_client() -> None:
    """Reset to None (used between test modules if needed)."""
    global _client
    _client = None


def _redact(url: str) -> str:
    """Hide credentials in ``redis://user:pass@host`` for logs."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"
    return url
