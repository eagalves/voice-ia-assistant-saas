"""Redis Stream client abstraction — Issues #3 and #13.

Two implementations of the same ``EventStreamClient`` protocol:

* ``FakeEventStream`` — pure-Python in-memory implementation for unit tests
  and single-process development (no Redis required, no durability, no
  cross-container visibility).
* ``RedisEventStream`` — thin adapter over ``redis.asyncio`` used whenever
  ``REDIS_URL`` is configured (see ``stream_client.get_stream_client``).

Every key passed to these methods MUST be constructed via ``redis_keys.py``
helpers so the ``{tenant_id}`` hash-tag invariant from Issue #10 is
respected.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventStreamClient(Protocol):
    """Minimal Redis interface required by AudioGateway and routers."""

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

    async def delete(self, key: str) -> None:
        """Delete a key (string or stream)."""
        ...

    async def publish(self, channel: str, message: str) -> None:
        """Publish a message to a pub/sub channel."""
        ...

    def subscribe(self, channel: str) -> AsyncIterator[str]:
        """Yield messages published to *channel* (never returns on its own)."""
        ...


class FakeEventStream:
    """In-memory EventStream for unit tests and single-process dev.

    Safe for use in a single asyncio event loop.  No Redis process required.

    Stream IDs have the format ``<ms>-<seq>``, mirroring Redis Streams.
    ID comparisons are done numerically (as (ms, seq) tuples) so that
    ``"T-2" < "T-10" < "T-100"`` — the same ordering Redis uses.
    """

    # Poll interval of the in-memory ``subscribe`` generator (seconds).
    SUBSCRIBE_POLL_S: float = 0.1

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._kv: dict[str, str] = {}
        self._ttls: dict[str, int] = {}
        self._seq: int = 0
        self._published: dict[str, list[str]] = {}  # channel → list[message]

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

    async def delete(self, key: str) -> None:
        """Delete a string key or stream key."""
        self._kv.pop(key, None)
        self._streams.pop(key, None)
        self._ttls.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        """Append message to the in-memory pub/sub log."""
        self._published.setdefault(channel, []).append(message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        """Yield messages published to *channel* after subscription time.

        Polls the in-memory log; mirrors the semantics of a Redis
        ``SUBSCRIBE`` (only messages published after subscribing are seen).
        """
        seen_up_to = len(self._published.get(channel, []))
        while True:
            messages = self._published.get(channel, [])
            if len(messages) > seen_up_to:
                for msg in messages[seen_up_to:]:
                    yield msg
                seen_up_to = len(messages)
            await asyncio.sleep(self.SUBSCRIBE_POLL_S)

    # ----------------------------------------------------------------
    # Test helpers
    # ----------------------------------------------------------------

    def ttl_for(self, key: str) -> int | None:
        """Return the TTL registered for a key (None if not set)."""
        return self._ttls.get(key)

    def stream_entries(self, key: str) -> list[tuple[str, dict[str, str]]]:
        """Return a copy of all stream entries for a key."""
        return list(self._streams.get(key, []))

    def published_messages(self, channel: str) -> list[str]:
        """Return all messages published to *channel* (for test assertions)."""
        return list(self._published.get(channel, []))


class RedisEventStream:
    """``EventStreamClient`` backed by ``redis.asyncio`` (Issue #13).

    ``decode_responses=True`` so every value crosses the boundary as ``str``,
    matching ``FakeEventStream``.  Construct with ``RedisEventStream(url)``;
    the connection pool is created lazily on first use.
    """

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self._url = url
        self._client = client

    def _redis(self):
        if self._client is None:
            import redis.asyncio as redis_asyncio  # deferred import

            self._client = redis_asyncio.from_url(self._url, decode_responses=True)
        return self._client

    async def ping(self) -> bool:
        return bool(await self._redis().ping())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        id: str = "*",
        nx: bool = False,
    ) -> str | None:
        # Redis has no XADD NX for explicit IDs; a duplicate explicit ID raises.
        try:
            return await self._redis().xadd(key, fields, id=id)
        except Exception as exc:  # noqa: BLE001
            if nx and "equal or smaller" in str(exc):
                return None
            raise

    async def xrevrange(
        self,
        key: str,
        max_id: str = "+",
        min_id: str = "-",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        rows = await self._redis().xrevrange(key, max=max_id, min=min_id, count=count)
        return [(str(eid), dict(fields)) for eid, fields in rows]

    async def xrange(
        self,
        key: str,
        min_id: str = "-",
        max_id: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        rows = await self._redis().xrange(key, min=min_id, max=max_id, count=count)
        return [(str(eid), dict(fields)) for eid, fields in rows]

    async def xlen(self, key: str) -> int:
        return int(await self._redis().xlen(key))

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await self._redis().set(key, value, ex=ex)

    async def get(self, key: str) -> str | None:
        value = await self._redis().get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, seconds: int) -> None:
        await self._redis().expire(key, seconds)

    async def delete(self, key: str) -> None:
        await self._redis().delete(key)

    async def publish(self, channel: str, message: str) -> None:
        await self._redis().publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        pubsub = self._redis().pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    await asyncio.sleep(0)  # yield control; keeps cancellation responsive
                    continue
                data = msg.get("data")
                if data is None:
                    continue
                yield data if isinstance(data, str) else data.decode()
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
