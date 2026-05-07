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


# ── microphone source (sounddevice) ────────────────────────────────────────────


class MicrophoneSource(AudioSource):
    """Live microphone source backed by ``sounddevice``.

    Tries the requested sample rate first; on PortAudio "Invalid sample rate"
    (PaErrorCode -9997, common on Windows WASAPI), falls back to a list of
    rates the device is more likely to accept and resamples downstream.

    Audio always exits ``read_chunk`` as 16-bit PCM mono at ``sample_rate``
    (the rate actually opened) — STT does the final resample to 16 kHz.
    """

    channels = 1

    DEFAULT_FALLBACK_RATES = (16000, 48000, 44100, 32000, 22050)

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_frames: int = 1024,
        device: int | str | None = None,
        fallback_rates: Iterable[int] | None = None,
        debug: bool = False,
    ) -> None:
        self._requested_rate = sample_rate
        self.sample_rate = sample_rate
        self.chunk_frames = chunk_frames
        self._device = device
        self._fallback_rates = tuple(fallback_rates or self.DEFAULT_FALLBACK_RATES)
        self._debug = debug
        self._stream = None
        self._queue: "object" = None  # queue.Queue[bytes]; lazily built

    def open(self) -> None:
        import queue

        import sounddevice as sd  # deferred import — keeps tests light

        self._queue = queue.Queue(maxsize=256)

        def _callback(indata, frames, time_info, status):  # noqa: ARG001
            if status and self._debug:
                print(f"[mic] status={status}", flush=True)
            try:
                # indata is a CFFI buffer of int16; copy to bytes for thread safety
                self._queue.put_nowait(bytes(indata))
            except Exception:
                # drop chunk rather than block the audio thread
                pass

        rates_to_try = (self._requested_rate, *self._fallback_rates)
        last_exc: Exception | None = None
        for rate in dict.fromkeys(rates_to_try):  # dedupe, preserve order
            try:
                self._stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=self.chunk_frames,
                    device=self._device,
                    dtype="int16",
                    channels=self.channels,
                    callback=_callback,
                )
                self._stream.start()
                self.sample_rate = rate
                if self._debug:
                    print(f"[mic] opened device={self._device} rate={rate}", flush=True)
                return
            except Exception as exc:  # noqa: BLE001 — PortAudioError lives in a sub-package
                last_exc = exc
                if self._debug:
                    print(f"[mic] rate {rate} rejected: {exc}", flush=True)
                self._stream = None
        raise RuntimeError(
            f"Could not open microphone with any sample rate {rates_to_try}: {last_exc}"
        )

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def read_chunk(self) -> bytes:
        if self._queue is None:  # pragma: no cover — open() failed
            return b""
        try:
            return self._queue.get(timeout=1.0)
        except Exception:
            # Return empty bytes signals "no audio yet" — caller decides what to do.
            return b""

    def source_name(self) -> str:
        return "microphone"

    @staticmethod
    def list_devices() -> list[dict]:
        """Return a flat list of input-capable devices for ``--list-devices``."""
        import sounddevice as sd

        devices = sd.query_devices()
        out: list[dict] = []
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            out.append(
                {
                    "index": idx,
                    "name": dev.get("name", "?"),
                    "host_api": dev.get("hostapi", 0),
                    "default_samplerate": int(dev.get("default_samplerate", 0)),
                    "max_input_channels": dev.get("max_input_channels", 0),
                }
            )
        return out
