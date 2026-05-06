from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

# ── data contracts ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AudioSegment:
    """A captured speech segment with origin metadata."""

    audio_bytes: bytes
    sample_rate: int
    channels: int
    source: str  # e.g. "microphone", "file", "test"
    timestamp: float = field(default_factory=time.time)


# ── capture state ──────────────────────────────────────────────────────────────


class CaptureState:
    IDLE = "idle"
    LISTENING = "listening"
    ERROR = "error"


# ── VAD interface (injectable) ─────────────────────────────────────────────────


class VADPolicy:
    """Decide whether a raw audio chunk contains speech.

    Override ``is_speech`` in tests/subclasses to inject behaviour.
    """

    def is_speech(self, chunk: bytes, sample_rate: int) -> bool:  # pragma: no cover
        """Return True if *chunk* contains speech."""
        raise NotImplementedError


class AmplitudeVAD(VADPolicy):
    """Simple energy-based VAD — suitable for development and testing."""

    def __init__(self, threshold: float = 500.0):
        self.threshold = threshold

    def is_speech(self, chunk: bytes, sample_rate: int) -> bool:
        if not chunk:
            return False
        # interpret as 16-bit signed PCM
        import array
        samples = array.array("h", chunk)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return rms >= self.threshold


# ── audio source interface ─────────────────────────────────────────────────────


class AudioSource:
    """Yields raw PCM chunks.  Subclass for microphone or test sources."""

    sample_rate: int = 16000
    channels: int = 1
    chunk_frames: int = 1024

    def open(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def read_chunk(self) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def source_name(self) -> str:
        return "unknown"


# ── AudioCapture ───────────────────────────────────────────────────────────────


class AudioCapture:
    """Control audio capture and emit segmented speech chunks.

    Usage::

        def on_segment(seg: AudioSegment) -> None:
            ...

        cap = AudioCapture(source=MicrophoneSource(), vad=AmplitudeVAD())
        cap.start(on_segment)
        # ... later ...
        cap.stop()

    ``on_segment`` is called from the capture thread; keep it fast.
    """

    def __init__(
        self,
        source: AudioSource,
        vad: VADPolicy,
        silence_chunks_to_flush: int = 10,
    ) -> None:
        self._source = source
        self._vad = vad
        self._silence_chunks_to_flush = silence_chunks_to_flush
        self._state: str = CaptureState.IDLE
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── public ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def start(self, on_segment: Callable[[AudioSegment], None]) -> None:
        """Start capturing.  Raises ``RuntimeError`` if already listening."""
        if self._state == CaptureState.LISTENING:
            raise RuntimeError("AudioCapture is already listening.")
        self._stop_event.clear()
        self._state = CaptureState.LISTENING
        self._thread = threading.Thread(
            target=self._run,
            args=(on_segment,),
            daemon=True,
            name="audio-capture",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal capture to stop and wait for thread to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._state = CaptureState.IDLE

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self, on_segment: Callable[[AudioSegment], None]) -> None:
        try:
            self._source.open()
            speech_buffer: list[bytes] = []
            silence_count = 0

            while not self._stop_event.is_set():
                chunk = self._source.read_chunk()
                if not chunk:
                    break

                if self._vad.is_speech(chunk, self._source.sample_rate):
                    speech_buffer.append(chunk)
                    silence_count = 0
                else:
                    if speech_buffer:
                        silence_count += 1
                        speech_buffer.append(chunk)  # include trailing silence
                        if silence_count >= self._silence_chunks_to_flush:
                            self._emit(speech_buffer, on_segment)
                            speech_buffer = []
                            silence_count = 0

            # flush remaining speech on stop
            if speech_buffer:
                self._emit(speech_buffer, on_segment)

        except Exception as exc:  # noqa: BLE001
            self._state = CaptureState.ERROR
            raise
        finally:
            self._source.close()
            if self._state == CaptureState.LISTENING:
                self._state = CaptureState.IDLE

    def _emit(
        self,
        chunks: list[bytes],
        on_segment: Callable[[AudioSegment], None],
    ) -> None:
        audio_bytes = b"".join(chunks)
        seg = AudioSegment(
            audio_bytes=audio_bytes,
            sample_rate=self._source.sample_rate,
            channels=self._source.channels,
            source=self._source.source_name(),
        )
        on_segment(seg)
