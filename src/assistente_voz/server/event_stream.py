"""Redis Stream client abstraction — Issue #3.

Production uses ``redis.asyncio`` (not yet a declared dependency — add
``redis[hiredis]>=5.0`` to ``pyproject.toml`` before Fase 2 go-live).

Tests inject ``FakeEventStream`` — pure-Python in-memory implementation,
no external process required.

Every key passed to these methods MUST be constructed via ``redis_keys.py``
helpers so the ``{tenant_id}`` hash-tag invariant from Issue #10 is
respected.
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class EventStreamClient(Protocol):
    """Minimal Redis interface required by AudioGateway."""

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        id: str = "*",
        nx: bool = False,
    ) -> str | None:
        """Append entry to stream.  Returns assigned stream ID or None (NX miss)."""
        ...

    async def xrevrange(
        self,
        key: str,
        max_id: str = "+",
        min_id: str = "-",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        """Return entries in reverse chronological order."""
        ...

    async def xrange(
        self,
        key: str,
        min_id: str = "-",
        max_id: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        """Return entries in chronological order."""
        ...

    async def xlen(self, key: str) -> int:
        """Return number of entries in stream."""
        ...

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Set a string value with optional TTL in seconds."""
        ...

    async def get(self, key: str) -> str | None:
        """Get a string value; None if absent."""
        ...

    async def expire(self, key: str, seconds: int) -> None:
        """Set TTL on an existing key."""
        ...


class FakeEventStream:
    """In-memory EventStream for unit tests.

    Thread-safe for use in a single asyncio event loop.  No Redis process
    required.

    Stream IDs have the format ``<ms>-<seq>``, mirroring Redis Streams.
    ID comparisons are done numerically (as (ms, seq) tuples) so that
    ``"T-2" < "T-10" < "T-100"`` — the same ordering Redis uses.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._kv: dict[str, str] = {}
        self._ttls: dict[str, int] = {}
        self._seq: int = 0

    # ----------------------------------------------------------------
    # ID helpers — numeric (ms, seq) comparison
    # ----------------------------------------------------------------

    def _next_id(self) -> str:
        self._seq += 1
        return f"{int(time.time() * 1000)}-{self._seq}"

    @staticmethod
    def _parse_id(id_str: str) -> tuple[int, int]:
        """Parse ``"<ms>-<seq>"`` → ``(ms, seq)`` for numeric comparison."""
        parts = id_str.split("-", 1)
        if len(parts) != 2:
            return (0, 0)
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return (0, 0)

    def _id_ge(self, a: str, b: str) -> bool:
        return self._parse_id(a) >= self._parse_id(b)

    def _id_le(self, a: str, b: str) -> bool:
        return self._parse_id(a) <= self._parse_id(b)

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        id: str = "*",
        nx: bool = False,
    ) -> str | None:
        stream = self._streams.setdefault(key, [])
        entry_id = id if id != "*" else self._next_id()
        if nx and any(eid == entry_id for eid, _ in stream):
            return None
        stream.append((entry_id, dict(fields)))
        return entry_id

    async def xrevrange(
        self,
        key: str,
        max_id: str = "+",
        min_id: str = "-",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        entries = list(reversed(self._streams.get(key, [])))
        if max_id != "+":
            entries = [(eid, f) for eid, f in entries if self._id_le(eid, max_id)]
        if min_id != "-":
            entries = [(eid, f) for eid, f in entries if self._id_ge(eid, min_id)]
        if count is not None:
            entries = entries[:count]
        return entries

    async def xrange(
        self,
        key: str,
        min_id: str = "-",
        max_id: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        entries = self._streams.get(key, [])
        if min_id != "-":
            entries = [(eid, f) for eid, f in entries if self._id_ge(eid, min_id)]
        if max_id != "+":
            entries = [(eid, f) for eid, f in entries if self._id_le(eid, max_id)]
        if count is not None:
            entries = entries[:count]
        return list(entries)

    async def xlen(self, key: str) -> int:
        return len(self._streams.get(key, []))

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._kv[key] = value
        if ex is not None:
            self._ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def expire(self, key: str, seconds: int) -> None:
        self._ttls[key] = seconds

    # ----------------------------------------------------------------
    # Test helpers
    # ----------------------------------------------------------------

    def ttl_for(self, key: str) -> int | None:
        """Return the TTL registered for a key (None if not set)."""
        return self._ttls.get(key)

    def stream_entries(self, key: str) -> list[tuple[str, dict[str, str]]]:
        """Return a copy of all stream entries for a key."""
        return list(self._streams.get(key, []))
