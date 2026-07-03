from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol, Union, runtime_checkable

_logger = logging.getLogger(__name__)

# faster-whisper accepts float32 numpy arrays in [-1, 1] at 16 kHz, mono.
# We allow callers to pass either raw int16 PCM bytes (legacy path) or a
# preprocessed float32 ndarray (streaming path) — see ``_to_float32``.
AudioInput = Union[bytes, "object"]  # ndarray typed as object to avoid hard numpy dep here

# ── data contracts ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    confidence: float          # 0.0–1.0 (avg segment confidence from Whisper)
    language: str
    source: str = "stt"


class TranscriptionStatus(Enum):
    OK = auto()            # confidence >= threshold, text ready
    LOW_CONFIDENCE = auto()  # retries exhausted, needs manual fallback
    EMPTY = auto()         # no speech detected


@dataclass
class STTOutcome:
    status: TranscriptionStatus
    result: TranscriptionResult | None = None
    attempts: int = 0


# ── Whisper backend interface (injectable) ─────────────────────────────────────


@runtime_checkable
class WhisperBackend(Protocol):
    """Minimal interface consumed by STTService — allows test doubles."""

    def transcribe(
        self,
        audio: AudioInput,
        language: str = "pt",
        sample_rate: int | None = None,
    ) -> tuple[str, float]:
        """Return (text, avg_confidence).  confidence is 0.0–1.0.

        ``sample_rate`` is the capture rate of *audio* when passed as raw bytes.
        Implementations should resample to 16 kHz when it differs from their
        expected input rate.  Pass ``None`` when audio is already float32 @ 16 kHz.
        """
        ...


# ── audio coercion helper ─────────────────────────────────────────────────────


def _lowpass_fir(arr, sample_rate: int, cutoff_hz: float = 7000.0, numtaps: int = 121):
    """Anti-alias low-pass (windowed-sinc FIR) applied before downsampling.

    Without this, energy above 8 kHz (mic hiss, keyboard clicks) folds back
    into the speech band during 48/44.1 kHz → 16 kHz conversion and degrades
    Whisper accuracy. Uses FFT convolution for large buffers.
    """
    import numpy as np

    nyquist = sample_rate / 2.0
    norm = min(cutoff_hz / nyquist, 0.999)
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = norm * np.sinc(norm * n)
    h *= np.hanning(numtaps)
    h /= h.sum()

    if arr.size * numtaps > 4_000_000:  # FFT path for long buffers
        full = arr.size + numtaps - 1
        nfft = 1 << (full - 1).bit_length()
        out = np.fft.irfft(np.fft.rfft(arr, nfft) * np.fft.rfft(h, nfft), nfft)[:full]
        start = (numtaps - 1) // 2
        return out[start:start + arr.size].astype(np.float32)
    return np.convolve(arr, h, mode="same").astype(np.float32)


def _to_float32(audio: AudioInput, sample_rate: int = 16000):
    """Coerce the caller's audio buffer into a float32 ndarray in [-1, 1] @ 16 kHz.

    Accepts:
      - bytes: interpreted as 16-bit signed little-endian PCM mono at *sample_rate*.
      - numpy.ndarray of float32 [-1, 1]: returned as-is.
      - numpy.ndarray of int16: scaled to float32.
    Downsampling applies an anti-alias FIR low-pass before linear interpolation;
    this keeps the pipeline dependency-free (no scipy/librosa) while avoiding
    HF-noise aliasing into the speech band.
    """
    import numpy as np  # deferred — keeps import cost out of unit tests

    if isinstance(audio, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
    else:
        arr = np.asarray(audio)
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        elif arr.dtype != np.float32:
            arr = arr.astype(np.float32)

    if arr.ndim > 1:
        arr = arr.mean(axis=1)  # downmix to mono

    if sample_rate != 16000 and arr.size > 0:
        if sample_rate > 16000 and arr.size > 121:
            arr = _lowpass_fir(arr, sample_rate)
        ratio = 16000 / float(sample_rate)
        new_len = int(round(arr.shape[0] * ratio))
        if new_len > 0:
            x_old = np.linspace(0.0, 1.0, num=arr.shape[0], endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)

    return np.ascontiguousarray(arr, dtype=np.float32)


def _condition_audio(arr, target_peak: float = 0.95, max_gain: float = 40.0):
    """Remove DC offset and normalise quiet captures to a healthy peak level.

    Whisper's log-mel front-end is not level-invariant: audio captured at a low
    mic gain (common on Windows laptops) transcribes measurably worse. Gain is
    capped so digital silence / noise floors are never blown up into
    hallucination fodder.
    """
    import numpy as np

    if arr.size == 0:
        return arr
    mean = float(arr.mean())
    if abs(mean) > 1e-3:
        arr = arr - mean
    peak = float(np.max(np.abs(arr)))
    if 1e-4 < peak < target_peak:
        arr = arr * min(target_peak / peak, max_gain)
    return np.ascontiguousarray(arr, dtype=np.float32)


DEFAULT_TEMPERATURE_LADDER: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def parse_temperature_env(raw: str | None) -> float | tuple[float, ...]:
    """Parse WHISPER_TEMPERATURE: single float, CSV ladder, or default ladder.

    Invalid values fall back to the default ladder with a warning — a typo in
    .env must never disable the decode-failure safety net silently.
    """
    if raw is None or not raw.strip():
        return DEFAULT_TEMPERATURE_LADDER
    try:
        values = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError:
        _logger.warning(
            "WHISPER_TEMPERATURE=%r is not a float or CSV of floats; "
            "using default ladder %s", raw, DEFAULT_TEMPERATURE_LADDER,
        )
        return DEFAULT_TEMPERATURE_LADDER
    if not values:
        return DEFAULT_TEMPERATURE_LADDER
    return values[0] if len(values) == 1 else values


# ── pt-BR hallucination guard ─────────────────────────────────────────────────

# Whisper models hallucinate YouTube/subtitle boilerplate on silence or noise.
# Matching is done on accent-stripped, punctuation-free, lowercase text.
# Tier 1: never legitimate output — always dropped (substring match).
_PT_HALLUCINATION_ALWAYS = ("amaraorg", "legendas pela comunidade")
# Tier 2: plausible-but-suspect closers — dropped only when Whisper itself
# flags the segment as probably-not-speech.
_PT_HALLUCINATION_SUSPECT = frozenset({
    "obrigado por assistir",
    "obrigado por assistir ate o proximo video",
    "ate o proximo video",
    "inscrevase no canal",
    "nao se esqueca de se inscrever no canal",
    "curta e compartilhe",
    "tchau tchau",
})


def _normalize_for_match(text: str) -> str:
    import re
    import unicodedata

    stripped = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = re.sub(r"[^a-z0-9 ]+", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _is_hallucinated_segment(text: str, no_speech_prob: float | None) -> bool:
    norm = _normalize_for_match(text)
    if not norm:
        return False
    if any(marker in norm for marker in _PT_HALLUCINATION_ALWAYS):
        return True
    return norm in _PT_HALLUCINATION_SUSPECT and (no_speech_prob or 0.0) >= 0.5


# ── Whisper model cache (singleton per process) ───────────────────────────────

# Key: (model_size, device, compute_type, cpu_threads) — the parameters that
# determine which model/runtime is instantiated. beam_size / temperature etc.
# are inference-time parameters and do not affect the loaded model.
_model_cache: dict[tuple[str, str, str, int], object] = {}
_model_cache_lock = threading.Lock()


def _get_cached_model(
    model_size: str, device: str, compute_type: str, cpu_threads: int = 0
) -> object:
    """Return a WhisperModel for the given config, loading it once per process.

    Thread-safe: concurrent callers with the same key block until the first
    load completes, then receive the cached instance.
    """
    cache_key = (model_size, device, compute_type, cpu_threads)

    # Fast path — model already cached (no lock needed for read after first load).
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    with _model_cache_lock:
        # Re-check inside lock: another thread may have loaded while we waited.
        if cache_key in _model_cache:
            return _model_cache[cache_key]

        from faster_whisper import WhisperModel  # deferred import

        _logger.info(
            "WhisperModel: loading '%s' (device=%s compute=%s)",
            model_size, device, compute_type,
        )
        t0 = time.perf_counter()
        model = WhisperModel(
            model_size, device=device, compute_type=compute_type, cpu_threads=cpu_threads
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _logger.info(
            "WhisperModel: '%s' loaded in %.0f ms — cached for this process",
            model_size, elapsed_ms,
        )
        _model_cache[cache_key] = model

    return _model_cache[cache_key]


def clear_model_cache() -> None:
    """Evict all cached WhisperModel instances.

    Intended for tests and tooling only. In production the cache lives for
    the entire process lifetime; clearing it forces a reload on next use.
    """
    with _model_cache_lock:
        _model_cache.clear()


# ── default backend using faster-whisper ──────────────────────────────────────


class FasterWhisperBackend:
    """Production backend.  Model is loaded lazily on first use and cached per process.

    Instances with the same (model_size, device, compute_type) share the same
    WhisperModel object — no reload across sessions within the same process.

    ``input_sample_rate`` is the sample rate of *raw bytes* passed to
    ``transcribe``.  Internally everything is coerced to float32 @ 16 kHz —
    the rate Whisper expects.

    ``vad_filter`` controls whether we ask Whisper's internal Silero VAD to
    re-trim the buffer.  In the streaming path we already segmented with our
    own VAD, so leaving this on doubles up and frequently drops short words —
    keep it OFF for live captioning.  Turn it ON for long, unsegmented files
    (batch/API path) where it both speeds up decoding and prevents silence
    hallucinations.

    ``temperature`` accepts the faster-whisper fallback ladder: when a decode
    fails quality checks (compression_ratio / avg_logprob), the model retries
    at the next temperature instead of returning garbage.  A single 0.0 value
    disables that safety net — only do it for latency-critical partials.

    ``initial_prompt`` primes the decoder with domain vocabulary (e.g. drug
    names, clinical terms) and the expected register — one of the highest-
    leverage accuracy knobs for pt-BR medical speech.  ``hotwords`` is a
    lighter-weight variant for a handful of critical terms.
    """

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cpu",
        compute_type: str = "int8",
        input_sample_rate: int = 16000,
        beam_size: int = 5,
        vad_filter: bool = False,
        condition_on_previous_text: bool = False,
        no_speech_threshold: float = 0.6,
        log_prob_threshold: float = -1.0,
        temperature: float | tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        compression_ratio_threshold: float = 2.4,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        cpu_threads: int = 0,
        condition_audio: bool = True,
        filter_hallucinations: bool = True,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._input_sample_rate = input_sample_rate
        self._beam_size = beam_size
        self._vad_filter = vad_filter
        self._condition_on_previous_text = condition_on_previous_text
        self._no_speech_threshold = no_speech_threshold
        self._log_prob_threshold = log_prob_threshold
        self._temperature = temperature
        self._compression_ratio_threshold = compression_ratio_threshold
        self._initial_prompt = initial_prompt
        self._hotwords = hotwords
        self._cpu_threads = cpu_threads
        self._condition_audio = condition_audio
        self._filter_hallucinations = filter_hallucinations
        # Normalized prompt for echo suppression: on silence, Whisper tends to
        # parrot the initial_prompt back as if it had been spoken.
        self._prompt_norm = _normalize_for_match(initial_prompt) if initial_prompt else ""
        self._model = None  # local reference; populated from process-level cache on first use

    def _load(self):
        if self._model is None:
            self._model = _get_cached_model(
                self._model_size, self._device, self._compute_type, self._cpu_threads
            )
        return self._model

    def transcribe(
        self,
        audio: AudioInput,
        language: str = "pt",
        sample_rate: int | None = None,
    ) -> tuple[str, float]:
        model = self._load()
        effective_rate = sample_rate if sample_rate is not None else self._input_sample_rate
        samples = _to_float32(audio, sample_rate=effective_rate)
        if samples.size == 0:
            return "", 0.0
        if self._condition_audio:
            samples = _condition_audio(samples)

        segments, info = model.transcribe(
            samples,
            language=language,
            beam_size=self._beam_size,
            vad_filter=self._vad_filter,
            condition_on_previous_text=self._condition_on_previous_text,
            no_speech_threshold=self._no_speech_threshold,
            log_prob_threshold=self._log_prob_threshold,
            temperature=self._temperature,
            compression_ratio_threshold=self._compression_ratio_threshold,
            initial_prompt=self._initial_prompt,
            hotwords=self._hotwords,
            word_timestamps=False,
        )

        texts: list[str] = []
        seg_confidences: list[float] = []
        no_speech_probs: list[float] = []
        for seg in segments:
            t = seg.text.strip()
            no_sp = getattr(seg, "no_speech_prob", None)
            if t and self._filter_hallucinations and _is_hallucinated_segment(t, no_sp):
                _logger.debug("FasterWhisperBackend: dropped hallucinated segment %r", t)
                continue
            if (
                t
                and self._filter_hallucinations
                and self._prompt_norm
                and (no_sp or 0.0) >= 0.4
                and _normalize_for_match(t) in self._prompt_norm
            ):
                # Prompt echo: the decoder emitted (part of) the initial_prompt
                # over a buffer it itself flags as probably-not-speech.
                _logger.debug("FasterWhisperBackend: dropped prompt echo %r", t)
                continue
            if t:
                texts.append(t)
            avg_logprob = getattr(seg, "avg_logprob", None)
            if avg_logprob is not None:
                # exp(avg_logprob) is a calibrated 0–1 probability — much more
                # consistent than 1 + logprob with a hard clamp.
                seg_confidences.append(math.exp(avg_logprob))
            if no_sp is not None:
                no_speech_probs.append(no_sp)

        text = " ".join(texts)
        if not seg_confidences:
            return text, 0.0
        avg_conf = sum(seg_confidences) / len(seg_confidences)
        if no_speech_probs:
            # Penalise by the worst no-speech probability across segments —
            # Whisper raises this when it suspects the buffer is silence/noise.
            avg_conf *= 1.0 - min(0.95, max(no_speech_probs))
        return text, max(0.0, min(1.0, avg_conf))


# ── STT service ────────────────────────────────────────────────────────────────


class STTService:
    """Transcribe audio with confidence policy, retries, and fallback.

    Policy (from PRD):
    - confidence >= threshold  →  OK
    - confidence < threshold   →  retry (up to max_retries)
    - after max_retries        →  LOW_CONFIDENCE (manual fallback required)
    """

    def __init__(
        self,
        backend: WhisperBackend,
        language: str = "pt",
        confidence_threshold: float = 0.80,
        max_retries: int = 2,
    ) -> None:
        self._backend = backend
        self._language = language
        self._threshold = confidence_threshold
        self._max_retries = max_retries

    def transcribe(
        self,
        audio: AudioInput,
        sample_rate: int | None = None,
    ) -> STTOutcome:
        """Attempt transcription applying confidence policy.

        ``audio`` may be raw 16-bit PCM bytes or a numpy float32 array @ 16 kHz.
        ``sample_rate`` must be provided when ``audio`` is raw bytes captured at
        a rate other than 16 kHz (e.g. after Windows fallback to 48 kHz).
        STTService resamples to float32 @ 16 kHz before calling the backend so
        every attempt in the retry loop receives audio in the correct format.
        """
        # Normalise to float32 @ 16 kHz once, before the retry loop, so every
        # attempt operates on the same resampled buffer.
        if sample_rate is not None and sample_rate != 16000:
            audio = _to_float32(audio, sample_rate=sample_rate)
        elif sample_rate is None and isinstance(audio, (bytes, bytearray, memoryview)):
            _logger.warning(
                "STTService.transcribe: raw bytes received without sample_rate — "
                "assuming 16 kHz. If audio was captured at a different rate "
                "(e.g. after Windows fallback), pass sample_rate=segment.sample_rate "
                "to avoid silent transcription quality loss."
            )

        attempts = 0
        last_result: TranscriptionResult | None = None

        for attempt in range(1, self._max_retries + 2):  # 1 initial + max_retries
            attempts = attempt
            text, confidence = self._backend.transcribe(audio, language=self._language)

            if not text.strip():
                return STTOutcome(
                    status=TranscriptionStatus.EMPTY,
                    result=None,
                    attempts=attempts,
                )

            last_result = TranscriptionResult(
                text=text, confidence=confidence, language=self._language
            )

            if confidence >= self._threshold:
                return STTOutcome(
                    status=TranscriptionStatus.OK,
                    result=last_result,
                    attempts=attempts,
                )

        # exhausted retries
        return STTOutcome(
            status=TranscriptionStatus.LOW_CONFIDENCE,
            result=last_result,
            attempts=attempts,
        )

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "STTService":
        """Build an STTService from environment variables.

        WHISPER_MODEL          — model size (default: small)
        WHISPER_DEVICE         — cpu or cuda (default: cpu)
        WHISPER_COMPUTE        — int8 / int8_float16 / float16 (default: int8)
        WHISPER_BEAM_SIZE      — beam width (default: 5)
        WHISPER_CPU_THREADS    — CTranslate2 intra-op threads; 0 = library default
        WHISPER_TEMPERATURE    — single value ("0.0") or CSV fallback ladder
                                 ("0.0,0.2,0.4"); default: full ladder
        WHISPER_INITIAL_PROMPT — decoder priming text (domain vocabulary)
        WHISPER_HOTWORDS       — comma/space separated critical terms
        WHISPER_VAD_FILTER     — "1"/"true" enables internal Silero VAD
                                 (for unsegmented audio; keep off for live path)
        STT_LANGUAGE           — BCP-47 language code (default: pt)
        STT_CONFIDENCE         — threshold 0.0–1.0 (default: 0.55)
        STT_MAX_RETRIES        — integer (default: 1)
        """
        model_size = os.getenv("WHISPER_MODEL", "small")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE", "int8")
        beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
        language = os.getenv("STT_LANGUAGE", "pt")
        threshold = float(os.getenv("STT_CONFIDENCE", "0.55"))
        max_retries = int(os.getenv("STT_MAX_RETRIES", "1"))

        return cls(
            backend=FasterWhisperBackend(
                model_size=model_size,
                device=device,
                compute_type=compute_type,
                beam_size=beam_size,
                cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "0")),
                temperature=parse_temperature_env(os.getenv("WHISPER_TEMPERATURE")),
                initial_prompt=os.getenv("WHISPER_INITIAL_PROMPT") or None,
                hotwords=os.getenv("WHISPER_HOTWORDS") or None,
                vad_filter=os.getenv("WHISPER_VAD_FILTER", "").strip().lower()
                in ("1", "true", "yes", "on"),
            ),
            language=language,
            confidence_threshold=threshold,
            max_retries=max_retries,
        )
