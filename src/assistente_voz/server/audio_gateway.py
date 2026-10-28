"""AudioGateway — WebSocket audio ingestion layer — Issues #3, #13, #14.

One ``AudioGateway`` instance per WebSocket connection (one speaker stream
per Sessão).  Manages:

- Bounded in-memory frame queue (50 frames = 1 s at Opus 20 ms)
- ``seq`` tracking and late-arrival detection (200 ms watermark)
- Push to Redis Streams with ``server_timestamp_ms``
- Server-side deduplication: ``event_id`` is **deterministic**
  (``uuid5(session_id, "{speaker}:{seq}")``) and a per-speaker set of seen
  ``seq`` values (lazily loaded from the stream on reconnect) drops resent
  frames — the at-least-once guarantee of the reconnect protocol
- Reconnect handshake: returns ``last_seq`` and ``last_event_id`` from Stream
- Periodic state snapshot (100 events / 5 min; 25 events / 1 min for medical)
- Snapshot + delta restore after crash
- Optional consumer task draining the frame queue into a handler
  (``SessionTranscriber`` in production); without a consumer frames are
  logged to the stream and dropped from the STT queue once it is full
  (``STT_OVERFLOW``)
- ``record_transcript`` — appends a ``type=transcript`` entry (text +
  confidence) to the same stream that the draft materializer reads

Opus decoding is injectable (``OpusDecoderLike`` protocol).  Tests inject
``PassthroughDecoder``; production code can plug in ``opuslib`` when
available (frames are treated as PCM16 LE @ 16 kHz until then).

Redis is injected as ``EventStreamClient``.  Tests use ``FakeEventStream``;
production wires ``RedisEventStream`` (``REDIS_URL``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .drain import get_drain_manager
from .event_stream import EventStreamClient, FakeEventStream
from .metrics import events_late_arrival_ratio_gauge
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

ENTRY_TYPE_FRAME = "frame"
ENTRY_TYPE_TRANSCRIPT = "transcript"

# Namespace for deterministic frame event_ids (uuid5).
_EVENT_ID_NAMESPACE = uuid.UUID("5b2c0b6e-1b1f-4e8f-9a4e-3f1d0c7a9e11")


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
    until ``opuslib`` is added to the production dependencies — clients
    send PCM16 LE @ 16 kHz mono frames meanwhile.
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
    duplicate: bool = False  # frame already in the stream (resent after reconnect)


FrameHandler = Callable[[AudioFrame], Awaitable[None]]


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

        # Dedupe (per speaker) — lazily loaded from the stream on first frame
        self._seen_seqs: set[int] | None = None

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
        self._duplicate_count: int = 0

        # Consumer
        self._consumer_task: asyncio.Task | None = None

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

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    # ── Dedupe ───────────────────────────────────────────────────────────────

    def frame_event_id(self, seq: int) -> str:
        """Deterministic event_id for (session, speaker, seq)."""
        return str(uuid.uuid5(_EVENT_ID_NAMESPACE, f"{self.session_id}:{self.speaker}:{seq}"))

    async def _ensure_seen_loaded(self) -> None:
        if self._seen_seqs is not None:
            return
        seen: set[int] = set()
        entries = await self._stream.xrange(self._events_key())
        for _, fields in entries:
            if fields.get("type", ENTRY_TYPE_FRAME) != ENTRY_TYPE_FRAME:
                continue
            if fields.get("speaker") != self.speaker:
                continue
            try:
                seen.add(int(fields.get("seq", -1)))
            except ValueError:
                continue
        self._seen_seqs = seen

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

        ``duplicate=True`` means a frame with this ``seq`` (same speaker) is
        already in the stream — it is neither queued nor logged again.

        Late arrival is determined by comparing ``timestamp_ms`` to the
        rolling watermark: if ``timestamp_ms < watermark − 200 ms`` the
        frame is marked ``late_arrival=True``.
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        await self._ensure_seen_loaded()
        assert self._seen_seqs is not None
        event_id = self.frame_event_id(seq)

        if seq in self._seen_seqs:
            self._duplicate_count += 1
            return ProcessResult(
                pcm_bytes=b"",
                event_id=event_id,
                stream_id=None,
                late_arrival=False,
                overflow=False,
                duplicate=True,
            )

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
        self._seen_seqs.add(seq)

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
            "type": ENTRY_TYPE_FRAME,
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

        events_late_arrival_ratio_gauge.set(self.late_arrival_ratio, tenant_id=self.tenant_id)

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

    # ── Transcript events ────────────────────────────────────────────────────

    async def record_transcript(
        self,
        *,
        event_id: str,
        text: str,
        confidence: float,
        uncertain: bool,
        timestamp_ms: int,
        seq_from: int | None = None,
        seq_to: int | None = None,
        red_flags: list[str] | None = None,
    ) -> str | None:
        """Append a ``transcript`` entry (text + metadata) to the Sessão stream."""
        fields = {
            "type": ENTRY_TYPE_TRANSCRIPT,
            "event_id": event_id,
            "speaker": self.speaker,
            "text": text,
            "confidence": f"{confidence:.4f}",
            "uncertain": str(bool(uncertain)).lower(),
            "server_timestamp_ms": str(timestamp_ms),
            "seq_from": str(seq_from if seq_from is not None else -1),
            "seq_to": str(seq_to if seq_to is not None else -1),
            "red_flags": ",".join(red_flags or []),
        }
        return await self._stream.xadd(self._events_key(), fields)

    # ── Consumer ─────────────────────────────────────────────────────────────

    def start_consumer(self, handler: FrameHandler) -> asyncio.Task:
        """Drain the frame queue into *handler* in a background task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            raise RuntimeError("consumer already running")
        self._consumer_task = asyncio.create_task(self._consume(handler))
        return self._consumer_task

    async def _consume(self, handler: FrameHandler) -> None:
        while True:
            frame = await self._queue.get()
            try:
                await handler(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad frame must not kill the stream
                logger.exception("AudioGateway consumer: handler failed for seq=%s", frame.seq)
            finally:
                self._queue.task_done()

    async def stop_consumer(self) -> None:
        task = self._consumer_task
        self._consumer_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # ── Reconnect handshake ──────────────────────────────────────────────────

    async def reconnect_handshake(self) -> dict:
        """Return the handshake payload for a reconnecting client.

        Reads the most recent *frame* entry of this speaker from the Redis
        Stream to get ``last_seq`` and ``last_event_id``. The client should
        resend only frames with ``seq > last_seq``.

        Returns::

            {
                "type": "session_resumed",
                "last_event_id": "<redis-stream-id>",
                "last_seq": 42,
            }
        """
        entries = await self._stream.xrevrange(self._events_key())
        for last_stream_id, fields in entries:
            if fields.get("type", ENTRY_TYPE_FRAME) != ENTRY_TYPE_FRAME:
                continue
            if fields.get("speaker", self.speaker) != self.speaker:
                continue
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

    @staticmethod
    def _frame_entry(entry_id: str, fields: dict[str, str]) -> dict | None:
        if fields.get("type", ENTRY_TYPE_FRAME) != ENTRY_TYPE_FRAME:
            return None
        return {
            "event_id": fields.get("event_id", ""),
            "seq": int(fields.get("seq", -1)),
            "timestamp_ms": int(fields.get("server_timestamp_ms", 0)),
            "late_arrival": fields.get("late_arrival", "false") == "true",
            "stream_id": entry_id,
        }

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
                frame = self._frame_entry(entry_id, fields)
                if frame is not None:
                    self._events.append(frame)
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
                frame = self._frame_entry(entry_id, fields)
                if frame is not None:
                    self._events.append(frame)
            self._event_count = len(self._events)
            if self._events:
                self._last_seq = self._events[-1]["seq"]
                self._last_event_id = self._events[-1]["stream_id"]

        return True

    # ── Drain integration ────────────────────────────────────────────────────

    def register_drain_callback(
        self, conn_id: str, send_drain: Callable[[], Awaitable[None]]
    ) -> None:
        """Register ``send_drain`` with the global DrainManager."""
        get_drain_manager().register(conn_id, send_drain)

    def unregister_drain_callback(self, conn_id: str) -> None:
        get_drain_manager().unregister(conn_id)

    # ── Session close ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Mark session as closed: set STREAM_TTL_SECONDS TTL on the stream key.

        ``POST /v1/sessions/{id}/close`` sets the same TTL through
        ``session_lifecycle.close_session_tx``; this method keeps the
        gateway-side contract for callers that own a gateway instance.
        """
        await self.set_stream_ttl()

    async def set_stream_ttl(self) -> None:
        """Set 24h TTL on the Redis Stream after session close."""
        await self._stream.expire(self._events_key(), STREAM_TTL_SECONDS)
