"""STT streaming service with confidence threshold and circuit breaker — Issues #4, #7, #14.

Architecture
------------
Primary backend:  Deepgram Nova-2 streaming pt-BR (StreamingSTTBackend protocol).
Fallback backend: OpenAI Whisper API batch (BatchSTTBackend protocol).

Neither managed provider is wired yet (needs DPA + credentials — Issue #12).
What exists today, selected by ``STT_SERVER_BACKEND`` through
``build_stt_service``:

* ``fake``          — deterministic test doubles (``FakeStreamingBackend`` /
                      ``FakeBatchBackend``);
* ``local_whisper`` — the MVP ``FasterWhisperBackend`` (CPU) exposed as a
                      batch backend; the "streaming" side is a
                      ``BatchOnlyStreamingBackend`` adapter so the circuit
                      breaker and status events keep the same semantics.

Confidence policy
-----------------
``STT_CONFIDENCE`` env var (range [0.30, 0.95]).  Absent or out-of-range →
warning log + effective 0.55.  Segments below threshold receive
``uncertain=True`` and, when building the draft, the caller must apply
``apply_incerto_prefix()`` to mark them for medical review.

Circuit breaker (Deepgram → Whisper)
--------------------------------------
- **Open:** N=3 consecutive Deepgram failures (timeout ≥ 800 ms OR connection
  error).
- **Open state:** all chunks routed to Whisper batch; WS status event
  ``{"type":"status","stt_mode":"fallback_batch"}`` emitted via on_ws_status.
- **Minimum open window:** 60 s (no recovery attempt before then).
- **Close:** 5 consecutive health-check successes → WS event
  ``{"type":"status","stt_mode":"streaming"}``.

Medical completude-first
------------------------
When ``is_medical=True`` and ``MEDICAL_STT_PARTIAL_ENABLED`` is falsy
(default), the ``on_partial`` callback is never invoked — only ``on_final``.

Test doubles (FakeStreamingBackend / FakeBatchBackend) are shipped here so
tests can import them without setting up real Deepgram/Whisper credentials.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from .metrics import (
    stt_circuit_state_gauge,
    stt_fallback_total,
    stt_segment_duration_ms_histogram,
)

_logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

STT_CONFIDENCE_DEFAULT: float = 0.55
STT_CONFIDENCE_MIN: float = 0.30
STT_CONFIDENCE_MAX: float = 0.95

DEEPGRAM_TIMEOUT_S: float = 0.8      # per-chunk timeout for streaming backend
CB_FAILURE_THRESHOLD: int = 3        # consecutive failures to open circuit breaker
CB_HEALTH_THRESHOLD: int = 5         # consecutive OK probes to close circuit breaker
CB_MIN_OPEN_DURATION_S: float = 60.0  # minimum seconds in open state before recovery

# Local CPU Whisper is batch by nature; a 4 s window takes seconds on CPU.
LOCAL_WHISPER_TIMEOUT_S: float = 120.0

BACKEND_OFF = "off"
BACKEND_FAKE = "fake"
BACKEND_LOCAL_WHISPER = "local_whisper"
SUPPORTED_BACKENDS: tuple[str, ...] = (BACKEND_OFF, BACKEND_FAKE, BACKEND_LOCAL_WHISPER)


# ── CircuitBreakerConfig ──────────────────────────────────────────────────────

@dataclass
class CircuitBreakerConfig:
    """Runtime configuration for the STT circuit breaker.

    All values can be overridden via environment variables so that SREs can
    tune thresholds without a code deploy.
    """

    failure_threshold: int = 3               # N consecutive failures to open
    fallback_min_window_s: int = 60          # min seconds in fallback before probing
    health_check_success_required: int = 5   # consecutive OK probes to close
    probe_interval_s: int = 10               # seconds between health probes
    timeout_per_segment_ms: int = 800        # Deepgram per-chunk timeout

    @classmethod
    def from_env(cls) -> "CircuitBreakerConfig":
        """Load config from env vars; invalid values fall back to defaults."""

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                _logger.warning(
                    "CircuitBreakerConfig: %s=%r is not a valid int; using default %d",
                    name,
                    raw,
                    default,
                )
                return default

        return cls(
            failure_threshold=_int("CB_FAILURE_THRESHOLD", 3),
            fallback_min_window_s=_int("CB_FALLBACK_MIN_WINDOW_S", 60),
            health_check_success_required=_int("CB_HEALTH_CHECK_SUCCESS_REQUIRED", 5),
            probe_interval_s=_int("CB_PROBE_INTERVAL_S", 10),
            timeout_per_segment_ms=_int("CB_TIMEOUT_PER_SEGMENT_MS", 800),
        )

    @property
    def timeout_s(self) -> float:
        """Deepgram per-segment timeout in seconds."""
        return self.timeout_per_segment_ms / 1000.0


# ── Confidence threshold helper ───────────────────────────────────────────────

def get_confidence_threshold(raw_value: str | None = None) -> float:
    """Validate STT_CONFIDENCE and return a safe float in [0.30, 0.95].

    If *raw_value* is None the function reads ``os.getenv("STT_CONFIDENCE")``.
    Any value that is absent, non-numeric, or outside the valid range causes
    a warning log and the default (0.55) to be returned — the session is
    never interrupted by a misconfigured threshold.
    """
    if raw_value is None:
        raw_value = os.getenv("STT_CONFIDENCE")

    if raw_value is None:
        _logger.warning(
            "STT_CONFIDENCE not set; using default %.2f", STT_CONFIDENCE_DEFAULT
        )
        return STT_CONFIDENCE_DEFAULT

    try:
        value = float(raw_value)
    except ValueError:
        _logger.warning(
            "STT_CONFIDENCE=%r is not a valid float; using default %.2f",
            raw_value,
            STT_CONFIDENCE_DEFAULT,
        )
        return STT_CONFIDENCE_DEFAULT

    if not (STT_CONFIDENCE_MIN <= value <= STT_CONFIDENCE_MAX):
        _logger.warning(
            "STT_CONFIDENCE=%.2f is outside [%.2f, %.2f]; using default %.2f",
            value,
            STT_CONFIDENCE_MIN,
            STT_CONFIDENCE_MAX,
            STT_CONFIDENCE_DEFAULT,
        )
        return STT_CONFIDENCE_DEFAULT

    return value


# ── STTEvent ──────────────────────────────────────────────────────────────────

@dataclass
class STTEvent:
    """Single transcription event produced by either streaming or batch backend."""

    text: str
    confidence: float        # 0.0 – 1.0
    is_final: bool
    event_id: str            # UUID v4 for downstream dedupe
    speaker: str | None      # "doctor" | "patient" | None
    uncertain: bool          # True if confidence < STT_CONFIDENCE threshold


# ── Draft helpers ─────────────────────────────────────────────────────────────

def apply_incerto_prefix(text: str) -> str:
    """Prepend ``[incerto]`` to *text* if ``uncertain=True`` and not already prefixed.

    Used by the draft materialiser (Issue #5) when building prontuário fields
    from events with ``uncertain=True``.
    """
    if text.startswith("[incerto]"):
        return text
    return f"[incerto] {text}"


# ── Backend Protocols ─────────────────────────────────────────────────────────

class StreamingSTTBackend(Protocol):
    """Deepgram-like real-time streaming backend."""

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        speaker: str | None,
        on_partial: Callable[[STTEvent], None] | None,
        on_final: Callable[[STTEvent], None],
        sample_rate: int = 16000,
    ) -> None:
        """Process an async iterable of audio chunks.

        Invokes *on_partial* for interim results (may be None).
        Invokes *on_final* for each final segment.
        Raises on connection error; silently cancels on timeout.
        """
        ...

    async def health_check(self) -> bool:
        """Lightweight connectivity probe; returns True if Deepgram is reachable."""
        ...


class BatchSTTBackend(Protocol):
    """Whisper-like offline batch backend."""

    async def transcribe_batch(
        self,
        audio_bytes: bytes,
        speaker: str | None = None,
        sample_rate: int = 16000,
    ) -> STTEvent:
        """Transcribe a single audio buffer and return one STTEvent."""
        ...


# ── Circuit breaker ───────────────────────────────────────────────────────────

class _CBState(Enum):
    CLOSED = auto()   # using streaming (Deepgram)
    OPEN = auto()     # using batch (Whisper)


class STTCircuitBreaker:
    """Tracks consecutive Deepgram failures and manages open/closed transitions.

    *clock* is injectable for tests so time can be advanced without sleeping.
    *config* overrides the module-level constants; defaults to
    ``CircuitBreakerConfig()`` when not supplied.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._clock = clock
        self._config = config or CircuitBreakerConfig()
        self._state = _CBState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_health_ok: int = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._state == _CBState.OPEN

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    def record_failure(self) -> bool:
        """Record one Deepgram failure.  Returns True if CB just opened."""
        self._consecutive_health_ok = 0
        if self._state == _CBState.CLOSED:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.failure_threshold:
                self._state = _CBState.OPEN
                self._opened_at = self._clock()
                self._consecutive_failures = 0
                stt_circuit_state_gauge.set(1.0)
                return True
        return False

    def record_success(self) -> None:
        """Record a Deepgram success; resets the failure counter."""
        self._consecutive_failures = 0

    def record_health_ok(self) -> bool:
        """Record a successful health probe.  Returns True if CB just closed.

        Only considers closing if the minimum open duration has elapsed.
        """
        if not self.is_open or self._opened_at is None:
            return False
        if self._clock() - self._opened_at < self._config.fallback_min_window_s:
            return False
        self._consecutive_health_ok += 1
        if self._consecutive_health_ok >= self._config.health_check_success_required:
            self._state = _CBState.CLOSED
            self._opened_at = None
            self._consecutive_health_ok = 0
            stt_circuit_state_gauge.set(0.0)
            return True
        return False

    def record_health_fail(self) -> None:
        """Record a failed health probe; resets the consecutive-OK counter."""
        self._consecutive_health_ok = 0


# ── STTStreamService ──────────────────────────────────────────────────────────

class STTStreamService:
    """Routes audio chunks to Deepgram (streaming) or Whisper (batch).

    Per-chunk logic:
    1. If circuit breaker is open → use Whisper batch.
    2. Otherwise attempt Deepgram with ``_timeout_s`` deadline.
    3. On timeout or any exception → record failure, route chunk to batch.
       If that failure opens the CB, fire ``on_ws_status`` callback.
    4. On success → mark segments uncertain if below threshold, call on_final.

    Medical partial suppression: when ``is_medical=True`` and
    ``medical_partial_enabled=False`` (default), the ``on_partial`` callback
    passed by the caller is suppressed so no partial events reach the WS.
    """

    def __init__(
        self,
        streaming_backend: StreamingSTTBackend,
        batch_backend: BatchSTTBackend,
        *,
        confidence_threshold: float = STT_CONFIDENCE_DEFAULT,
        is_medical: bool = False,
        medical_partial_enabled: bool = False,
        on_ws_status: Callable[[dict], None] | None = None,
        _circuit_breaker: STTCircuitBreaker | None = None,
        _timeout_s: float = DEEPGRAM_TIMEOUT_S,
        tenant_id: str | None = None,
    ) -> None:
        self._streaming = streaming_backend
        self._batch = batch_backend
        self._confidence_threshold = confidence_threshold
        self._is_medical = is_medical
        self._medical_partial_enabled = medical_partial_enabled
        self._on_ws_status = on_ws_status
        self._cb = _circuit_breaker or STTCircuitBreaker()
        self._timeout_s = _timeout_s
        self._tenant_id = tenant_id

    @property
    def is_degraded(self) -> bool:
        """True when circuit breaker is open and Whisper batch is in use."""
        return self._cb.is_open

    @property
    def circuit_breaker(self) -> STTCircuitBreaker:
        return self._cb

    def _metric_tenant(self) -> dict:
        return {"tenant_id": self._tenant_id} if self._tenant_id else {}

    async def process_chunk(
        self,
        audio_bytes: bytes,
        speaker: str | None = None,
        *,
        on_partial: Callable[[STTEvent], None] | None = None,
        on_final: Callable[[STTEvent], None],
        sample_rate: int = 16000,
    ) -> None:
        """Process one audio buffer through the appropriate backend.

        Always calls *on_final* exactly once per chunk (with one or more
        STTEvent objects for Deepgram streaming, one for Whisper batch).
        """
        # Medical completude-first: suppress partials unless explicitly enabled
        effective_on_partial = (
            None
            if (self._is_medical and not self._medical_partial_enabled)
            else on_partial
        )

        t0 = time.perf_counter()
        try:
            if self._cb.is_open:
                stt_fallback_total.inc(reason="circuit_open", **self._metric_tenant())
                await self._run_batch(audio_bytes, speaker, on_final, sample_rate)
                return

            # Try Deepgram streaming with timeout
            collected: list[STTEvent] = []

            async def _single_chunk() -> AsyncIterator[bytes]:
                yield audio_bytes

            try:
                await asyncio.wait_for(
                    self._streaming.stream(
                        _single_chunk(),
                        speaker,
                        effective_on_partial,
                        collected.append,
                        sample_rate,
                    ),
                    timeout=self._timeout_s,
                )
                self._cb.record_success()
                for event in collected:
                    event.uncertain = event.confidence < self._confidence_threshold
                    on_final(event)

            except Exception as exc:  # noqa: BLE001 — any backend failure → fallback
                reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else "connection"
                stt_fallback_total.inc(reason=reason, **self._metric_tenant())
                just_opened = self._cb.record_failure()
                if just_opened and self._on_ws_status is not None:
                    self._on_ws_status(
                        {"type": "status", "stt_mode": "fallback_batch", "reason": "circuit_open"}
                    )
                await self._run_batch(audio_bytes, speaker, on_final, sample_rate)
        finally:
            stt_segment_duration_ms_histogram.observe(
                (time.perf_counter() - t0) * 1000.0, **self._metric_tenant()
            )

    async def _run_batch(
        self,
        audio_bytes: bytes,
        speaker: str | None,
        on_final: Callable[[STTEvent], None],
        sample_rate: int = 16000,
    ) -> None:
        event = await self._batch.transcribe_batch(audio_bytes, speaker, sample_rate)
        event.uncertain = event.confidence < self._confidence_threshold
        on_final(event)

    async def run_health_probe(self) -> None:
        """Execute one Deepgram health probe.

        Call from a periodic background task (see ``run_probe_loop``) while
        the circuit breaker is open.  No-op when CB is already closed.
        """
        if not self._cb.is_open:
            return
        ok = await self._streaming.health_check()
        if ok:
            just_closed = self._cb.record_health_ok()
            if just_closed and self._on_ws_status is not None:
                self._on_ws_status(
                    {"type": "status", "stt_mode": "streaming", "reason": "circuit_closed"}
                )
        else:
            self._cb.record_health_fail()

    async def run_probe_loop(self, interval_s: float | None = None) -> None:
        """Periodic probe loop; cancel the task to stop it."""
        interval = interval_s if interval_s is not None else float(
            self._cb.config.probe_interval_s
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_health_probe()
            except Exception as exc:  # noqa: BLE001 — probe errors never kill the loop
                _logger.debug("STT health probe raised: %s", exc)
                self._cb.record_health_fail()


# ── Test doubles ──────────────────────────────────────────────────────────────

class FakeStreamingBackend:
    """Configurable fake Deepgram backend for unit tests.

    Set ``fail=True`` to simulate a connection error.
    Set ``timeout=True`` to simulate a timeout (sleeps 3600 s; will be
    cancelled by ``asyncio.wait_for``).
    ``partial_calls`` and ``final_calls`` record every invocation for
    assertion in tests.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        timeout: bool = False,
        result_text: str = "texto transcrito",
        result_confidence: float = 0.90,
    ) -> None:
        self.fail = fail
        self.timeout = timeout
        self.result_text = result_text
        self.result_confidence = result_confidence
        self.stream_call_count: int = 0
        self.health_ok: bool = True
        self.partial_calls: list[STTEvent] = []
        self.final_calls: list[STTEvent] = []

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        speaker: str | None,
        on_partial: Callable[[STTEvent], None] | None,
        on_final: Callable[[STTEvent], None],
        sample_rate: int = 16000,
    ) -> None:
        self.stream_call_count += 1
        if self.timeout:
            await asyncio.sleep(3600)  # cancelled by wait_for timeout
        if self.fail:
            raise ConnectionError("Deepgram connection failed (fake)")
        async for _ in audio_chunks:
            pass
        event = STTEvent(
            text=self.result_text,
            confidence=self.result_confidence,
            is_final=True,
            event_id=str(uuid.uuid4()),
            speaker=speaker,
            uncertain=False,
        )
        if on_partial is not None:
            on_partial(event)
            self.partial_calls.append(event)
        on_final(event)
        self.final_calls.append(event)

    async def health_check(self) -> bool:
        return self.health_ok


class FakeBatchBackend:
    """Configurable fake Whisper batch backend for unit tests."""

    def __init__(
        self,
        *,
        result_text: str = "texto batch",
        result_confidence: float = 0.85,
    ) -> None:
        self.result_text = result_text
        self.result_confidence = result_confidence
        self.call_count: int = 0

    async def transcribe_batch(
        self,
        audio_bytes: bytes,
        speaker: str | None = None,
        sample_rate: int = 16000,
    ) -> STTEvent:
        self.call_count += 1
        return STTEvent(
            text=self.result_text,
            confidence=self.result_confidence,
            is_final=True,
            event_id=str(uuid.uuid4()),
            speaker=speaker,
            uncertain=False,
        )


# ── Production-usable backends without managed providers (Issue #14) ──────────

class BatchOnlyStreamingBackend:
    """Serve the ``StreamingSTTBackend`` protocol from a batch backend.

    Lets the circuit breaker / status-event machinery run unchanged when the
    only transcriber available is batch (local Whisper). Each ``stream`` call
    concatenates the chunks and emits exactly one final event.
    """

    def __init__(self, batch: BatchSTTBackend) -> None:
        self._batch = batch

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        speaker: str | None,
        on_partial: Callable[[STTEvent], None] | None,
        on_final: Callable[[STTEvent], None],
        sample_rate: int = 16000,
    ) -> None:
        buf = b"".join([chunk async for chunk in audio_chunks])
        event = await self._batch.transcribe_batch(buf, speaker, sample_rate)
        on_final(event)

    async def health_check(self) -> bool:
        return True


class LocalWhisperBatchBackend:
    """``BatchSTTBackend`` over the MVP ``FasterWhisperBackend`` (CPU).

    The model call is blocking and CPU-bound, so it runs in a worker thread
    (``asyncio.to_thread``) to keep the event loop (WS frames, other
    Sessões) responsive. Model instances are cached per process by
    ``assistente_voz.stt``.
    """

    def __init__(self, backend=None, language: str = "pt") -> None:
        self._backend = backend
        self._language = language

    def _load(self):
        if self._backend is None:
            from assistente_voz.stt import FasterWhisperBackend  # deferred import

            self._backend = FasterWhisperBackend.from_env()
        return self._backend

    async def transcribe_batch(
        self,
        audio_bytes: bytes,
        speaker: str | None = None,
        sample_rate: int = 16000,
    ) -> STTEvent:
        backend = self._load()
        text, confidence = await asyncio.to_thread(
            backend.transcribe, audio_bytes, self._language, sample_rate
        )
        return STTEvent(
            text=text.strip(),
            confidence=float(confidence),
            is_final=True,
            event_id=str(uuid.uuid4()),
            speaker=speaker,
            uncertain=False,
        )


def medical_partial_enabled_from_env() -> bool:
    return os.getenv("MEDICAL_STT_PARTIAL_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def build_stt_service(
    backend_kind: str,
    *,
    is_medical: bool,
    on_ws_status: Callable[[dict], None] | None = None,
    tenant_id: str | None = None,
    medical_partial_enabled: bool | None = None,
) -> STTStreamService | None:
    """Build the server STT service for one WS connection, or ``None`` when off.

    ``backend_kind`` is ``STT_SERVER_BACKEND``: ``off`` | ``fake`` |
    ``local_whisper``. Unknown values raise ``ValueError`` (fail-fast at
    connection time, logged by the router).
    """
    kind = (backend_kind or BACKEND_OFF).strip().lower()
    if kind in {"", BACKEND_OFF, "none", "0", "false"}:
        return None
    if kind not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"STT_SERVER_BACKEND={backend_kind!r} not supported; use one of {SUPPORTED_BACKENDS}"
        )

    config = CircuitBreakerConfig.from_env()
    if kind == BACKEND_FAKE:
        streaming: StreamingSTTBackend = FakeStreamingBackend()
        batch: BatchSTTBackend = FakeBatchBackend()
        timeout_s = config.timeout_s
    else:  # local_whisper
        batch = LocalWhisperBatchBackend(language=os.getenv("STT_LANGUAGE", "pt"))
        streaming = BatchOnlyStreamingBackend(batch)
        timeout_s = LOCAL_WHISPER_TIMEOUT_S

    partial = (
        medical_partial_enabled
        if medical_partial_enabled is not None
        else medical_partial_enabled_from_env()
    )
    return STTStreamService(
        streaming,
        batch,
        confidence_threshold=get_confidence_threshold(),
        is_medical=is_medical,
        medical_partial_enabled=partial,
        on_ws_status=on_ws_status,
        _circuit_breaker=STTCircuitBreaker(config=config),
        _timeout_s=timeout_s,
        tenant_id=tenant_id,
    )
