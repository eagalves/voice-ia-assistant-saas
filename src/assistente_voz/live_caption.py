from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from assistente_voz.audio_capture import AudioSource
from assistente_voz.stt import FasterWhisperBackend, _to_float32


# ── tuning defaults (override per call site) ──────────────────────────────────

DEFAULT_FRAME_MS = 20             # VAD analysis frame
DEFAULT_PREROLL_MS = 400          # speech kept before VAD opens an utterance
DEFAULT_HANG_MS = 1200            # silence required to close an utterance (completude-first)
DEFAULT_OPEN_FRAMES = 2           # consecutive speech frames to open (lowered: catch quieter starts)
DEFAULT_PARTIAL_INTERVAL_MS = 700  # how often we publish a live caption
DEFAULT_MIN_PARTIAL_MS = 900       # don't transcribe sub-900ms buffers (avoids hallucinations)
DEFAULT_VAD_RMS = 250.0           # int16 RMS threshold — completude-first; raise if noisy
DEFAULT_MIN_CONF = 0.30           # drop partials below this confidence (suppress hallucinations)


@dataclass(frozen=True)
class CaptionEvent:
    """Live caption update — replaces whatever the UI is showing."""

    text: str
    is_final: bool
    confidence: float
    audio_ms: int


@dataclass
class _TranscribeJob:
    buf: bytes
    sample_rate: int
    is_final: bool
    seq: int  # monotonically increasing; partial workers drop stale ones


class StreamingTranscriber:
    """Producer-consumer pipeline for low-latency live captioning.

    Threads:
      1. Audio producer: reads ``source.read_chunk()`` into a bytes queue.
      2. VAD/segmenter: frames audio @ frame_ms, opens/closes utterances using
         energy-based VAD with pre-roll and hang-time. Pushes transcription
         jobs to the worker; never blocks on Whisper.
      3. Transcription worker: pulls jobs from a queue and runs Whisper.
         Stale partial jobs are dropped (debounce) — only the latest partial
         for an open utterance gets transcribed. Final jobs are always run.

    The caller provides ``on_event``; it is invoked from the worker thread.
    Keep it fast (queue or simple stdout write).
    """

    def __init__(
        self,
        source: AudioSource,
        final_backend: FasterWhisperBackend,
        partial_backend: Optional[FasterWhisperBackend] = None,
        on_event: Optional[Callable[[CaptionEvent], None]] = None,
        language: str = "pt",
        frame_ms: int = DEFAULT_FRAME_MS,
        preroll_ms: int = DEFAULT_PREROLL_MS,
        hang_ms: int = DEFAULT_HANG_MS,
        open_frames: int = DEFAULT_OPEN_FRAMES,
        partial_interval_ms: int = DEFAULT_PARTIAL_INTERVAL_MS,
        min_partial_ms: int = DEFAULT_MIN_PARTIAL_MS,
        vad_rms_threshold: float = DEFAULT_VAD_RMS,
        min_confidence: float = DEFAULT_MIN_CONF,
        debug_audio: bool = False,
    ) -> None:
        if on_event is None:
            raise ValueError("on_event is required")
        self._source = source
        self._partial = partial_backend  # None disables partial transcription
        self._final = final_backend
        self._on_event = on_event
        self._language = language
        self._frame_ms = frame_ms
        self._preroll_ms = preroll_ms
        self._hang_ms = hang_ms
        self._open_frames = open_frames
        self._partial_interval_ms = partial_interval_ms
        self._min_partial_ms = min_partial_ms
        self._vad_threshold = vad_rms_threshold
        self._min_confidence = min_confidence
        self._debug = debug_audio

        self._stop = threading.Event()
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=512)
        self._jobs_q: queue.Queue[_TranscribeJob] = queue.Queue(maxsize=64)
        self._latest_partial_seq = 0  # worker drops jobs with seq < this for partials
        self._seq_lock = threading.Lock()
        self._producer_thread: Optional[threading.Thread] = None
        self._segmenter_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._source.open()
        self._stop.clear()
        self._producer_thread = threading.Thread(
            target=self._produce, name="audio-producer", daemon=True
        )
        self._segmenter_thread = threading.Thread(
            target=self._segment, name="vad-segmenter", daemon=True
        )
        self._worker_thread = threading.Thread(
            target=self._transcribe_worker, name="stt-worker", daemon=True
        )
        self._producer_thread.start()
        self._segmenter_thread.start()
        self._worker_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in (self._producer_thread, self._segmenter_thread, self._worker_thread):
            if t and t.is_alive():
                t.join(timeout=timeout)
        try:
            self._source.close()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass

    # ── producer: pumps source.read_chunk() into the queue ───────────────────

    def _produce(self) -> None:
        while not self._stop.is_set():
            chunk = self._source.read_chunk()
            if not chunk:
                continue
            try:
                self._audio_q.put(chunk, timeout=0.5)
            except queue.Full:
                # back-pressure: drop oldest to keep the realtime path alive
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    pass

    # ── segmenter: VAD only.  Pushes jobs to worker, never blocks on Whisper ─

    def _segment(self) -> None:
        sr = self._source.sample_rate
        bytes_per_sample = 2  # int16
        frame_bytes = int(sr * self._frame_ms / 1000) * bytes_per_sample
        preroll_frames = max(1, self._preroll_ms // self._frame_ms)
        hang_frames = max(1, self._hang_ms // self._frame_ms)
        partial_interval_s = self._partial_interval_ms / 1000.0
        min_partial_bytes = int(sr * self._min_partial_ms / 1000) * bytes_per_sample

        carry = bytearray()
        preroll = deque(maxlen=preroll_frames)
        utterance: list[bytes] = []
        speech_run = 0
        silence_run = 0
        in_utterance = False
        last_partial_ts = 0.0

        def utterance_bytes() -> bytes:
            return b"".join(utterance)

        while not self._stop.is_set() or not self._audio_q.empty():
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            carry.extend(chunk)
            while len(carry) >= frame_bytes:
                frame = bytes(carry[:frame_bytes])
                del carry[:frame_bytes]

                rms = _rms_int16(frame)
                if self._debug:
                    print(f"[vad] rms={rms:.0f} thr={self._vad_threshold:.0f}", flush=True)

                is_speech = rms >= self._vad_threshold

                if not in_utterance:
                    preroll.append(frame)
                    if is_speech:
                        speech_run += 1
                        if speech_run >= self._open_frames:
                            in_utterance = True
                            utterance = list(preroll)
                            preroll.clear()
                            silence_run = 0
                            last_partial_ts = time.monotonic()
                    else:
                        speech_run = 0
                else:
                    utterance.append(frame)
                    if is_speech:
                        silence_run = 0
                    else:
                        silence_run += 1
                        if silence_run >= hang_frames:
                            buf = utterance_bytes()
                            self._enqueue_final(buf, sr)
                            utterance = []
                            in_utterance = False
                            silence_run = 0
                            speech_run = 0
                            continue

                if (
                    self._partial is not None
                    and in_utterance
                    and (time.monotonic() - last_partial_ts) >= partial_interval_s
                ):
                    buf = utterance_bytes()
                    if len(buf) >= min_partial_bytes:
                        self._enqueue_partial(buf, sr)
                    last_partial_ts = time.monotonic()

        # flush on stop
        if in_utterance and utterance:
            self._enqueue_final(b"".join(utterance), sr)

    # ── job dispatch: partials are debounced, finals always run ───────────────

    def _enqueue_partial(self, buf: bytes, sr: int) -> None:
        with self._seq_lock:
            self._latest_partial_seq += 1
            seq = self._latest_partial_seq
        try:
            self._jobs_q.put_nowait(_TranscribeJob(buf=buf, sample_rate=sr, is_final=False, seq=seq))
        except queue.Full:
            # transcription is the bottleneck — drop oldest job to keep up
            try:
                self._jobs_q.get_nowait()
                self._jobs_q.put_nowait(_TranscribeJob(buf=buf, sample_rate=sr, is_final=False, seq=seq))
            except (queue.Empty, queue.Full):
                pass

    def _enqueue_final(self, buf: bytes, sr: int) -> None:
        # Reset partial seq so any pending partials for this utterance are discarded.
        with self._seq_lock:
            self._latest_partial_seq += 1
            seq = self._latest_partial_seq
        try:
            self._jobs_q.put(_TranscribeJob(buf=buf, sample_rate=sr, is_final=True, seq=seq), timeout=2.0)
        except queue.Full:  # pragma: no cover
            pass

    # ── worker thread: runs Whisper, never touches audio queue ───────────────

    def _transcribe_worker(self) -> None:
        while not self._stop.is_set() or not self._jobs_q.empty():
            try:
                job = self._jobs_q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Debounce partials: skip if a newer one is already queued.
            if not job.is_final:
                with self._seq_lock:
                    if job.seq < self._latest_partial_seq:
                        continue

            backend = self._final if job.is_final else self._partial
            text, conf = _safe_transcribe(backend, job.buf, job.sample_rate, self._language)
            if not text:
                continue
            # Suppress likely-hallucinated low-confidence partials.
            if not job.is_final and conf < self._min_confidence:
                continue

            ms = int(len(job.buf) / 2 / job.sample_rate * 1000)
            self._on_event(
                CaptionEvent(text=text, is_final=job.is_final, confidence=conf, audio_ms=ms)
            )


# ── module-private helpers ─────────────────────────────────────────────────────


def _rms_int16(frame: bytes) -> float:
    if not frame:
        return 0.0
    import array

    samples = array.array("h", frame)
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def _safe_transcribe(
    backend: FasterWhisperBackend, buf: bytes, sr: int, language: str
) -> tuple[str, float]:
    """Coerce raw int16 PCM at *sr* into float32@16k and call the backend.

    Resampling is delegated to ``_to_float32``; we then feed the ndarray to
    the backend with the rate flag set to 16 kHz so it doesn't resample twice.
    """
    samples = _to_float32(buf, sample_rate=sr)
    original_rate = backend._input_sample_rate  # noqa: SLF001 — same module family
    try:
        backend._input_sample_rate = 16000  # noqa: SLF001
        text, conf = backend.transcribe(samples, language=language)
    except Exception as exc:  # noqa: BLE001
        return f"[stt-error: {type(exc).__name__}]", 0.0
    finally:
        backend._input_sample_rate = original_rate  # noqa: SLF001
    return text.strip(), conf
