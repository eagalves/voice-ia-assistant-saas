"""AudioGateway — WebSocket audio ingestion layer — Issue #3.

One ``AudioGateway`` instance per WebSocket connection (one speaker stream
per Sessão).  Manages:

- Bounded in-memory frame queue (50 frames = 1 s at Opus 20 ms)
- ``seq`` tracking and late-arrival detection (200 ms watermark)
- Push to Redis Streams with ``server_timestamp_ms``
- Server-side deduplication via ``event_id`` (XADD NX)
- Reconnect handshake: returns ``last_seq`` and ``last_event_id`` from Stream
- Periodic state snapshot (100 events / 5 min; 25 events / 1 min for medical)
- Snapshot + delta restore after crash

Opus decoding is injectable (``OpusDecoderLike`` protocol).  Tests inject
``PassthroughDecoder``; production code can plug in ``opuslib`` when
available.

Redis is injected as ``EventStreamClient``.  Tests use ``FakeEventStream``;
production wires ``redis.asyncio`` (add ``redis[hiredis]>=5.0`` to deps).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .drain import get_drain_manager
from .event_stream import EventStreamClient, FakeEventStream
from .redis_keys import session_events_key, session_state_key

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

# 1 s of 20 ms Opus frames
QUEUE_MAX_FRAMES: int = 50

# Late-arrival watermark window (ms)
LATE_ARRIVAL_WINDOW_MS: int = 200

# Snapshot thresholds — default (non-medical)
SNAPSHOT_EVENT_COUNT_DEFAULT: int = 100
SNAPSHOT_INTERVAL_S_DEFAULT: int = 300  # 5 min

# Snapshot thresholds — medical (4× more frequent)
SNAPSHOT_EVENT_COUNT_MEDICAL: int = 25
SNAPSHOT_INTERVAL_S_MEDICAL: int = 60   # 1 min

# Redis Stream TTL after session close
STREAM_TTL_SECONDS: int = 86400  # 24 h


# ── Opus decoder protocol ────────────────────────────────────────────────────

class OpusDecoderLike(Protocol):
    def decode(self, opus_bytes: bytes, frame_size: int = 320) -> bytes:
        """Decode one Opus frame → PCM int16 bytes.

        ``frame_size`` = 320 samples = 20 ms at 16 kHz mono.
        """
        ...


class PassthroughDecoder:
    """Returns the input unchanged.

    Used in tests (no native Opus library required) and as a stand-in
    until ``opuslib`` is added to the production dependencies.
    """

    def decode(self, opus_bytes: bytes, frame_size: int = 320) -> bytes:
        return opus_bytes


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AudioFrame:
    seq: int
    data: bytes        # decoded PCM int16 bytes
    timestamp_ms: int
    event_id: str


@dataclass
class ProcessResult:
    pcm_bytes: bytes
    event_id: str
    stream_id: str | None  # Redis Stream ID assigned (None if overflow)
    late_arrival: bool
    overflow: bool


# ── AudioGateway ─────────────────────────────────────────────────────────────

class AudioGateway:
    """Manages one WebSocket audio stream (one speaker per Sessão)."""

    def __init__(
        self,
        *,
        session_id: str,
        tenant_id: str,
        speaker: str = "doctor",
        stream_client: EventStreamClient | None = None,
        decoder: OpusDecoderLike | None = None,
        is_medical: bool = False,
    ) -> None:
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.speaker = speaker

        self._stream: EventStreamClient = stream_client or FakeEventStream()
        self._decoder: OpusDecoderLike = decoder or PassthroughDecoder()
        self._is_medical = is_medical

        # Frame queue — bounded to 1 s of audio
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(
            maxsize=QUEUE_MAX_FRAMES
        )

        # Sequence / watermark state
        self._last_seq: int = -1
        self._last_event_id: str = ""
        self._watermark_ms: int = 0

        # Event log (mirrors what's in the Redis Stream — used for snapshot)
        self._events: list[dict] = []
        self._event_count: int = 0

        # Snapshot scheduling
        self._snapshot_threshold = (
            SNAPSHOT_EVENT_COUNT_MEDICAL
            if is_medical
            else SNAPSHOT_EVENT_COUNT_DEFAULT
        )
        self._snapshot_interval = (
            SNAPSHOT_INTERVAL_S_MEDICAL
            if is_medical
            else SNAPSHOT_INTERVAL_S_DEFAULT
        )
        self._last_snapshot_at: float = time.monotonic()

        # Metrics
        self._late_arrival_count: int = 0
        self._total_frames_seen: int = 0

    # ── Keys ─────────────────────────────────────────────────────────────────

    def _events_key(self) -> str:
        return session_events_key(self.tenant_id, self.session_id)

    def _state_key(self) -> str:
        return session_state_key(self.tenant_id, self.session_id)

    # ── Metrics ──────────────────────────────────────────────────────────────

    @property
    def late_arrival_ratio(self) -> float:
        """Fraction of frames that were late arrivals (0.0–1.0)."""
        if self._total_frames_seen == 0:
            return 0.0
        return self._late_arrival_count / self._total_frames_seen

    # ── Core frame processing ────────────────────────────────────────────────

    async def process_frame(
        self,
        seq: int,
        frame_data: bytes,
        timestamp_ms: int | None = None,
    ) -> ProcessResult:
        """Process one incoming audio frame.

        Returns ``ProcessResult`` with ``overflow=True`` when the internal
        queue is full.  The caller must send an ``STT_OVERFLOW`` WS message
        and NOT close the WebSocket.

        Late arrival is determined by comparing ``timestamp_ms`` to the
        rolling watermark: if ``timestamp_ms < watermark − 200 ms`` the
        frame is marked ``late_arrival=True``.
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        # Decode Opus → PCM
        pcm = self._decoder.decode(frame_data)

        # Late-arrival detection
        late_arrival = False
        if (
            self._watermark_ms > 0
            and timestamp_ms < self._watermark_ms - LATE_ARRIVAL_WINDOW_MS
        ):
            late_arrival = True
            self._late_arrival_count += 1
        else:
            if timestamp_ms > self._watermark_ms:
                self._watermark_ms = timestamp_ms

        self._total_frames_seen += 1
        self._last_seq = seq

        event_id = str(uuid.uuid4())

        # Queue frame for local STT (bounded).  Overflow drops the frame from
        # the *STT pipeline* but we still record it in the Redis Stream so
        # reconnect / dedupe work correctly.
        overflow = self._queue.full()
        if not overflow:
            self._queue.put_nowait(
                AudioFrame(
                    seq=seq, data=pcm, timestamp_ms=timestamp_ms, event_id=event_id
                )
            )

        # Always push to Redis Stream (stream is the authoritative log)
        fields = {
            "seq": str(seq),
            "event_id": event_id,
            "speaker": self.speaker,
            "server_timestamp_ms": str(timestamp_ms),
            "late_arrival": str(late_arrival).lower(),
        }
        stream_id: str | None = await self._stream.xadd(self._events_key(), fields)
        if stream_id:
            self._last_event_id = stream_id
            self._events.append(
                {
                    "event_id": event_id,
                    "seq": seq,
                    "timestamp_ms": timestamp_ms,
                    "late_arrival": late_arrival,
                    "stream_id": stream_id,
                }
            )
            self._event_count += 1

        # Snapshot check
        if self._event_count > 0:
            now = time.monotonic()
            elapsed = now - self._last_snapshot_at
            if (
                self._event_count % self._snapshot_threshold == 0
                or elapsed >= self._snapshot_interval
            ):
                await self.snapshot()

        return ProcessResult(
            pcm_bytes=pcm,
            event_id=event_id,
            stream_id=stream_id,
            late_arrival=late_arrival,
            overflow=overflow,
        )
    # ── Reconnect handshake ──────────────────────────────────────────────────

    async def reconnect_handshake(self) -> dict:
        """Return the handshake payload for a reconnecting client.

        Reads the most recent Redis Stream entry to get ``last_seq`` and
        ``last_event_id``.  The client should resend only frames with
        ``seq > last_seq``.

        Returns::

            {
                "type": "session_resumed",
                "last_event_id": "<redis-stream-id>",
                "last_seq": 42,
            }
        """
        entries = await self._stream.xrevrange(self._events_key(), count=1)
        if entries:
            last_stream_id, fields = entries[0]
            last_seq = int(fields.get("seq", -1))
            return {
                "type": "session_resumed",
                "last_event_id": last_stream_id,
                "last_seq": last_seq,
            }
        return {"type": "session_resumed", "last_event_id": "", "last_seq": -1}

    # ── Snapshot ─────────────────────────────────────────────────────────────

    async def snapshot(self) -> None:
        """Persist current state to Redis for crash recovery."""
        state = {
            "last_seq": self._last_seq,
            "last_event_id": self._last_event_id,
            "events": list(self._events),
            "event_count": self._event_count,
        }
        await self._stream.set(
            self._state_key(),
            json.dumps(state),
            ex=STREAM_TTL_SECONDS,
        )
        self._last_snapshot_at = time.monotonic()
        logger.debug(
            "Snapshot written session=%s event_count=%d",
            self.session_id,
            self._event_count,
        )

    async def restore_from_snapshot(self) -> bool:
        """Load state snapshot + stream delta after a crash.

        Returns True if any state was restored; False if the stream is empty
        and no snapshot exists (fresh session).

        Algorithm:
        1. Read snapshot from Redis.
        2. If no snapshot: replay the full stream (up to 100 events).
        3. If snapshot: apply delta = stream entries after snapshot's
           ``last_event_id``.
        """
        raw = await self._stream.get(self._state_key())

        if raw is None:
            # No snapshot — full stream replay
            entries = await self._stream.xrange(self._events_key())
            if not entries:
                return False
            for entry_id, fields in entries:
                self._events.append(
                    {
                        "event_id": fields.get("event_id", ""),
                        "seq": int(fields.get("seq", -1)),
                        "timestamp_ms": int(
                            fields.get("server_timestamp_ms", 0)
                        ),
                        "late_arrival": fields.get("late_arrival", "false")
                        == "true",
                        "stream_id": entry_id,
                    }
                )
            self._event_count = len(self._events)
            if self._events:
                self._last_seq = self._events[-1]["seq"]
                self._last_event_id = self._events[-1]["stream_id"]
            return bool(self._events)

        # Restore from snapshot + delta
        state = json.loads(raw)
        self._last_seq = state["last_seq"]
        self._last_event_id = state["last_event_id"]
        self._events = state["events"]
        self._event_count = state["event_count"]

        if self._last_event_id:
            delta = await self._stream.xrange(
                self._events_key(), min_id=self._last_event_id
            )
            # Skip the first entry — it equals the snapshot's last_event_id
            for entry_id, fields in delta[1:]:
                self._events.append(
                    {
                        "event_id": fields.get("event_id", ""),
                        "seq": int(fields.get("seq", -1)),
                        "timestamp_ms": int(
                            fields.get("server_timestamp_ms", 0)
                        ),
                        "late_arrival": fields.get("late_arrival", "false")
                        == "true",
                        "stream_id": entry_id,
                    }
                )
            self._event_count = len(self._events)
            if self._events:
                self._last_seq = self._events[-1]["seq"]
                self._last_event_id = self._events[-1]["stream_id"]

        return True

    # ── Drain integration ────────────────────────────────────────────────────

    def register_drain_callback(
        self, conn_id: str, send_drain: "Callable[[], Awaitable[None]]"
    ) -> None:
        """Register ``send_drain`` with the global DrainManager."""
        get_drain_manager().register(conn_id, send_drain)

    def unregister_drain_callback(self, conn_id: str) -> None:
        get_drain_manager().unregister(conn_id)

    # ── Session close ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Mark session as closed: set STREAM_TTL_SECONDS TTL on the stream key.

        Called by the router after ``POST /v1/sessions/{id}/close`` succeeds.
        Delegates to ``set_stream_ttl()`` for a single source of truth.
        """
        await self.set_stream_ttl()

    async def set_stream_ttl(self) -> None:
        """Set 24h TTL on the Redis Stream after session close."""
        await self._stream.expire(self._events_key(), STREAM_TTL_SECONDS)
