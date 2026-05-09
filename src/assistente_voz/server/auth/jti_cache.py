"""JTI anti-replay cache — Issue #6.

Stores seen ``jti`` values for the duration of ``ttl_s`` (default 900s = 15min).
Eviction is lazy: expired entries are cleaned up on each ``mark()`` call.

The cache is in-memory and process-local.  For multi-container deployments
this must be backed by a shared Redis SET with TTL; that is out of scope for
Fase 2.

Clock is injectable for deterministic testing.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class JTICache:
    """In-process LRU-style JTI cache with TTL-based eviction."""

    def __init__(
        self,
        ttl_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._store: dict[str, float] = {}  # jti → expire_at (monotonic)
        self._lock = asyncio.Lock()

    async def seen(self, jti: str) -> bool:
        """Return True if *jti* has already been marked (replay detected)."""
        async with self._lock:
            expire_at = self._store.get(jti)
            if expire_at is None:
                return False
            if self._clock() > expire_at:
                # Already expired — treat as unseen (evict lazily)
                del self._store[jti]
                return False
            return True

    async def mark(self, jti: str) -> None:
        """Record *jti* as seen; evict expired entries from the store."""
        async with self._lock:
            now = self._clock()
            # Lazy eviction of expired entries
            expired = [k for k, exp in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
            self._store[jti] = now + self._ttl
