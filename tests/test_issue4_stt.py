"""Issue 4 – STT Local com Politica de Confianca e Fallback."""
import pytest

from assistente_voz.stt import (
    STTOutcome,
    STTService,
    TranscriptionResult,
    TranscriptionStatus,
    WhisperBackend,
)

# ── test doubles ──────────────────────────────────────────────────────────────


class StubBackend:
    """Returns a fixed (text, confidence) on each call."""

    def __init__(self, responses: list[tuple[str, float]]) -> None:
        self._responses = list(responses)
        self._calls = 0

    def transcribe(self, audio: bytes, language: str = "pt") -> tuple[str, float]:
        if self._calls >= len(self._responses):
            return self._responses[-1]
        result = self._responses[self._calls]
        self._calls += 1
        return result

    @property
    def call_count(self) -> int:
        return self._calls


AUDIO = b"\x00" * 1024  # dummy PCM bytes

# ── tests ─────────────────────────────────────────────────────────────────────


def test_high_confidence_returns_ok():
    """confidence >= 0.80 must produce OK on first attempt."""
    backend = StubBackend([("paciente relata dor de cabeca", 0.92)])
    svc = STTService(backend=backend, confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.status == TranscriptionStatus.OK
    assert outcome.result is not None
    assert outcome.result.text == "paciente relata dor de cabeca"
    assert outcome.result.confidence == 0.92
    assert outcome.attempts == 1


def test_low_confidence_triggers_retry_until_success():
    """One low-confidence followed by a passing attempt → OK with 2 attempts."""
    backend = StubBackend([
        ("algo meio ruido", 0.50),
        ("algo mais claro agora", 0.85),
    ])
    svc = STTService(backend=backend, confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.status == TranscriptionStatus.OK
    assert outcome.attempts == 2
    assert outcome.result is not None
    assert outcome.result.confidence == 0.85


def test_max_retries_exhausted_returns_low_confidence():
    """After max_retries all below threshold → LOW_CONFIDENCE fallback."""
    backend = StubBackend([
        ("ruido", 0.40),
        ("ainda ruido", 0.45),
        ("mais ruido", 0.50),
    ])
    svc = STTService(backend=backend, confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.status == TranscriptionStatus.LOW_CONFIDENCE
    assert outcome.attempts == 3  # 1 initial + 2 retries
    assert outcome.result is not None  # last result preserved for fallback UI


def test_empty_transcription_returns_empty_status():
    """Empty text from backend → EMPTY, no retries attempted."""
    backend = StubBackend([("", 0.0)])
    svc = STTService(backend=backend, confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.status == TranscriptionStatus.EMPTY
    assert outcome.result is None
    assert outcome.attempts == 1


def test_confidence_threshold_boundary_exactly_equal():
    """Confidence exactly equal to threshold must be accepted (>=)."""
    backend = StubBackend([("texto exato", 0.80)])
    svc = STTService(backend=backend, confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.status == TranscriptionStatus.OK


def test_result_preserves_language():
    backend = StubBackend([("texto", 0.90)])
    svc = STTService(backend=backend, language="pt", confidence_threshold=0.80, max_retries=2)
    outcome = svc.transcribe(AUDIO)

    assert outcome.result is not None
    assert outcome.result.language == "pt"


def test_stub_backend_satisfies_protocol():
    """StubBackend must satisfy the WhisperBackend Protocol."""
    assert isinstance(StubBackend([("x", 1.0)]), WhisperBackend)
