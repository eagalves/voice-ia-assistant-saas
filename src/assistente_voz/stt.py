from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol, runtime_checkable

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
        self, audio: bytes, language: str = "pt"
    ) -> tuple[str, float]:
        """Return (text, avg_confidence).  confidence is 0.0–1.0."""
        ...


# ── default backend using faster-whisper ──────────────────────────────────────


class FasterWhisperBackend:
    """Production backend.  Model is loaded lazily on first use."""

    def __init__(self, model_size: str = "medium", device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # deferred import

            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type="int8"
            )
        return self._model

    def transcribe(self, audio: bytes, language: str = "pt") -> tuple[str, float]:
        model = self._load()
        segments, info = model.transcribe(
            io.BytesIO(audio),
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        texts: list[str] = []
        confidences: list[float] = []
        for seg in segments:
            texts.append(seg.text.strip())
            # faster-whisper exposes avg_logprob; convert to 0–1 with sigmoid-like clamp
            raw_conf = getattr(seg, "avg_logprob", -1.0)
            conf = max(0.0, min(1.0, 1.0 + raw_conf))  # logprob 0 → 1.0, -1 → 0.0
            confidences.append(conf)

        text = " ".join(t for t in texts if t)
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return text, avg_conf


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

    def transcribe(self, audio: bytes) -> STTOutcome:
        """Attempt transcription applying confidence policy.

        ``audio`` must be 16-bit PCM or any format faster-whisper accepts.
        Retries by calling the backend again with the same audio — useful
        when the model is non-deterministic (beam search with sampling).
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

        WHISPER_MODEL      — model size (default: medium)
        WHISPER_DEVICE     — cpu or cuda (default: cpu)
        STT_LANGUAGE       — BCP-47 language code (default: pt)
        STT_CONFIDENCE     — threshold 0.0–1.0 (default: 0.80)
        STT_MAX_RETRIES    — integer (default: 2)
        """
        model_size = os.getenv("WHISPER_MODEL", "medium")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        language = os.getenv("STT_LANGUAGE", "pt")
        threshold = float(os.getenv("STT_CONFIDENCE", "0.80"))
        max_retries = int(os.getenv("STT_MAX_RETRIES", "2"))

        return cls(
            backend=FasterWhisperBackend(model_size=model_size, device=device),
            language=language,
            confidence_threshold=threshold,
            max_retries=max_retries,
        )
