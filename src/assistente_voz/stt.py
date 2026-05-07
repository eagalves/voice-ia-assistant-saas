from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol, Union, runtime_checkable

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
        self, audio: AudioInput, language: str = "pt"
    ) -> tuple[str, float]:
        """Return (text, avg_confidence).  confidence is 0.0–1.0."""
        ...


# ── audio coercion helper ─────────────────────────────────────────────────────


def _to_float32(audio: AudioInput, sample_rate: int = 16000):
    """Coerce the caller's audio buffer into a float32 ndarray in [-1, 1] @ 16 kHz.

    Accepts:
      - bytes: interpreted as 16-bit signed little-endian PCM mono at *sample_rate*.
      - numpy.ndarray of float32 [-1, 1]: returned as-is.
      - numpy.ndarray of int16: scaled to float32.
    Resampling to 16 kHz is done with linear interpolation when needed — adequate
    for STT and avoids pulling scipy/librosa.
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
        ratio = 16000 / float(sample_rate)
        new_len = int(round(arr.shape[0] * ratio))
        if new_len > 0:
            x_old = np.linspace(0.0, 1.0, num=arr.shape[0], endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)

    return np.ascontiguousarray(arr, dtype=np.float32)


# ── default backend using faster-whisper ──────────────────────────────────────


class FasterWhisperBackend:
    """Production backend.  Model is loaded lazily on first use.

    ``input_sample_rate`` is the sample rate of *raw bytes* passed to
    ``transcribe``.  Internally everything is coerced to float32 @ 16 kHz —
    the rate Whisper expects.

    ``vad_filter`` controls whether we ask Whisper's internal Silero VAD to
    re-trim the buffer.  In the streaming path we already segmented with our
    own VAD, so leaving this on doubles up and frequently drops short words —
    keep it OFF for live captioning.
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
        temperature: float = 0.0,
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
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # deferred import

            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        return self._model

    def transcribe(self, audio: AudioInput, language: str = "pt") -> tuple[str, float]:
        model = self._load()
        samples = _to_float32(audio, sample_rate=self._input_sample_rate)
        if samples.size == 0:
            return "", 0.0

        segments, info = model.transcribe(
            samples,
            language=language,
            beam_size=self._beam_size,
            vad_filter=self._vad_filter,
            condition_on_previous_text=self._condition_on_previous_text,
            no_speech_threshold=self._no_speech_threshold,
            log_prob_threshold=self._log_prob_threshold,
            temperature=self._temperature,
            word_timestamps=False,
        )

        texts: list[str] = []
        seg_confidences: list[float] = []
        no_speech_probs: list[float] = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                texts.append(t)
            avg_logprob = getattr(seg, "avg_logprob", None)
            if avg_logprob is not None:
                # exp(avg_logprob) is a calibrated 0–1 probability — much more
                # consistent than 1 + logprob with a hard clamp.
                seg_confidences.append(math.exp(avg_logprob))
            no_sp = getattr(seg, "no_speech_prob", None)
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

    def transcribe(self, audio: AudioInput) -> STTOutcome:
        """Attempt transcription applying confidence policy.

        ``audio`` may be raw 16-bit PCM bytes (legacy callers) or a numpy
        float32 array @ 16 kHz (streaming path).  Retries by calling the
        backend again — Whisper sampling is non-deterministic so a 2nd pass
        sometimes lifts a borderline buffer above the threshold.
        """
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

        WHISPER_MODEL      — model size (default: small)
        WHISPER_DEVICE     — cpu or cuda (default: cpu)
        WHISPER_COMPUTE    — int8 / int8_float16 / float16 (default: int8)
        WHISPER_BEAM_SIZE  — beam width (default: 5)
        STT_LANGUAGE       — BCP-47 language code (default: pt)
        STT_CONFIDENCE     — threshold 0.0–1.0 (default: 0.55)
        STT_MAX_RETRIES    — integer (default: 1)
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
            ),
            language=language,
            confidence_threshold=threshold,
            max_retries=max_retries,
        )
