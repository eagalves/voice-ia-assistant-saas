"""SessionTranscriber — frames → segments → STT → transcript events (Issue #14).

Sits between the ``AudioGateway`` frame queue and the ``STTStreamService``:

1. ``handle_frame`` accumulates decoded PCM16 (16 kHz mono) and tracks a
   cheap energy VAD (int16 RMS) per frame.
2. A segment is closed when either ``window_s`` of audio has accumulated or
   ``silence_ms`` of silence followed speech. Segments without any speech
   frame are discarded without calling the STT backend (no hallucinated
   "Obrigado por assistir" on silence).
3. Each closed segment is transcribed in its own task (bounded by
   ``max_inflight``) so the gateway queue keeps draining while CPU Whisper
   works; the consumer never blocks on STT.
4. Every final ``STTEvent`` with text is appended to the Sessão's Redis
   Stream as a ``transcript`` entry (``AudioGateway.record_transcript``) and
   pushed to the WebSocket client as ``{"type": "transcript", ...}``.
5. For ``medical`` Sessões on plans with ``llm_realtime_red_flag`` the
   transcript is scanned with the MVP red-flag list and a
   ``{"type": "red_flag", "terms": [...]}`` message is emitted immediately.

All WS writes go through the injected ``send`` coroutine (the router's
outbox) so a single task owns the socket.
"""
from __future__ import annotations

import array
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .audio_gateway import AudioFrame, AudioGateway
from .stt_service import STTEvent, STTStreamService

_logger = logging.getLogger(__name__)

Sender = Callable[[dict[str, Any]], Awaitable[None]]
RedFlagDetector = Callable[[str], list[str]]

BYTES_PER_SAMPLE = 2  # int16


def rms_int16(frame: bytes) -> float:
    if len(frame) < BYTES_PER_SAMPLE:
        return 0.0
    usable = frame[: len(frame) - (len(frame) % BYTES_PER_SAMPLE)]
    samples = array.array("h", usable)
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


class SessionTranscriber:
    """Per-WS-connection segmenter + STT dispatcher."""

    def __init__(
        self,
        *,
        gateway: AudioGateway,
        stt: STTStreamService,
        speaker: str,
        send: Sender,
        sample_rate: int = 16000,
        window_s: float = 4.0,
        silence_rms: float = 250.0,
        silence_ms: int = 800,
        max_inflight: int = 2,
        realtime_red_flags: bool = False,
        red_flag_detector: RedFlagDetector | None = None,
    ) -> None:
        self._gateway = gateway
        self._stt = stt
        self._speaker = speaker
        self._send = send
        self._sample_rate = sample_rate
        self._window_bytes = int(window_s * sample_rate) * BYTES_PER_SAMPLE
        self._silence_rms = silence_rms
        self._silence_bytes_needed = int(silence_ms / 1000.0 * sample_rate) * BYTES_PER_SAMPLE
        self._realtime_red_flags = realtime_red_flags
        self._detect = red_flag_detector

        self._buffer = bytearray()
        self._has_speech = False
        self._silence_bytes = 0
        self._seq_from: int | None = None
        self._seq_to: int | None = None
        self._last_timestamp_ms: int = 0

        self._semaphore = asyncio.Semaphore(max_inflight)
        self._jobs: set[asyncio.Task] = set()

        # Observability (per connection)
        self.segments_sent: int = 0
        self.segments_discarded: int = 0
        self.red_flags_sent: int = 0
        self.last_error: str | None = None

    # ── frame intake ─────────────────────────────────────────────────────────

    async def handle_frame(self, frame: AudioFrame) -> None:
        """Accumulate one decoded frame; close a segment when due."""
        if self._seq_from is None:
            self._seq_from = frame.seq
        self._seq_to = frame.seq
        self._last_timestamp_ms = frame.timestamp_ms
        self._buffer.extend(frame.data)

        if rms_int16(frame.data) >= self._silence_rms:
            self._has_speech = True
            self._silence_bytes = 0
        else:
            self._silence_bytes += len(frame.data)

        window_full = len(self._buffer) >= self._window_bytes
        silence_closed = self._has_speech and self._silence_bytes >= self._silence_bytes_needed
        if window_full or silence_closed:
            await self.flush()

    async def flush(self) -> None:
        """Close the current segment and dispatch it (if it contained speech)."""
        if not self._buffer:
            return
        pcm = bytes(self._buffer)
        seq_from, seq_to, ts = self._seq_from, self._seq_to, self._last_timestamp_ms
        self._buffer.clear()
        had_speech = self._has_speech
        self._has_speech = False
        self._silence_bytes = 0
        self._seq_from = None
        self._seq_to = None

        if not had_speech:
            self.segments_discarded += 1
            return

        task = asyncio.create_task(self._transcribe_segment(pcm, seq_from, seq_to, ts))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def aclose(self, timeout_s: float = 30.0) -> None:
        """Flush the trailing segment and wait for in-flight STT jobs."""
        await self.flush()
        if not self._jobs:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(self._jobs), return_exceptions=True), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "SessionTranscriber: %d STT job(s) still running after %.0fs; cancelling",
                len(self._jobs),
                timeout_s,
            )
            for job in list(self._jobs):
                job.cancel()

    # ── STT dispatch ─────────────────────────────────────────────────────────

    async def _transcribe_segment(
        self, pcm: bytes, seq_from: int | None, seq_to: int | None, timestamp_ms: int
    ) -> None:
        events: list[STTEvent] = []
        async with self._semaphore:
            t0 = time.perf_counter()
            try:
                await self._stt.process_chunk(
                    pcm,
                    self._speaker,
                    on_final=events.append,
                    sample_rate=self._sample_rate,
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft: the Sessão stays alive
                self.last_error = type(exc).__name__
                _logger.exception("SessionTranscriber: STT failed for segment")
                await self._send({"type": "status", "reason": "STT_SEGMENT_FAILED"})
                return
            latency_ms = (time.perf_counter() - t0) * 1000.0

        for event in events:
            text = (event.text or "").strip()
            if not text:
                continue
            flags = self._detect(text) if (self._realtime_red_flags and self._detect) else []
            stream_id = await self._gateway.record_transcript(
                event_id=event.event_id,
                text=text,
                confidence=event.confidence,
                uncertain=event.uncertain,
                timestamp_ms=timestamp_ms,
                seq_from=seq_from,
                seq_to=seq_to,
                red_flags=flags,
            )
            self.segments_sent += 1
            await self._send(
                {
                    "type": "transcript",
                    "event_id": event.event_id,
                    "stream_id": stream_id,
                    "speaker": self._speaker,
                    "text": text,
                    "confidence": round(float(event.confidence), 4),
                    "uncertain": bool(event.uncertain),
                    "is_final": True,
                    "seq_from": seq_from,
                    "seq_to": seq_to,
                    "latency_ms": round(latency_ms, 1),
                    "stt_mode": "fallback_batch" if self._stt.is_degraded else "streaming",
                }
            )
            if flags:
                self.red_flags_sent += 1
                await self._send(
                    {
                        "type": "red_flag",
                        "event_id": event.event_id,
                        "speaker": self._speaker,
                        "terms": flags,
                        "text": text,
                    }
                )
