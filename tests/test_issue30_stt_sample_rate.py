"""Issue 30 – STTService Direto Ignora Sample Rate do AudioSegment.

Garante que:
- STTService.transcribe(audio, sample_rate=48000) reamosta antes de chamar o backend.
- O backend sempre recebe audio float32 @ 16 kHz, independente da taxa original.
- WARNING emitido quando bytes chegam sem sample_rate (taxa desconhecida).
- Nenhum WARNING quando ndarray chega sem sample_rate (assumido pre-processado).
- FasterWhisperBackend.transcribe aceita sample_rate e o usa para _to_float32.
- Testes existentes de STTService nao regridem.
- WhisperBackend stub e compativel com a nova assinatura.
"""
from __future__ import annotations

import array
import logging

import pytest

from assistente_voz.stt import (
    STTOutcome,
    STTService,
    TranscriptionStatus,
    WhisperBackend,
    _to_float32,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _pcm_bytes(n_samples: int, value: int = 1000) -> bytes:
    return array.array("h", [value] * n_samples).tobytes()


# ── test double ───────────────────────────────────────────────────────────────


class CapturingBackend:
    """Records what transcribe() received and returns a fixed response."""

    def __init__(self, response: str = "ok", confidence: float = 0.9) -> None:
        self._response = response
        self._confidence = confidence
        self.received: list = []

    def transcribe(self, audio, language="pt", sample_rate=None):
        self.received.append(audio)
        return self._response, self._confidence


class StubBackend:
    """Minimal stub — same as in test_issue4_stt.py."""

    def __init__(self, responses=None):
        self._responses = list(responses or [("ok", 0.9)])
        self._index = 0

    def transcribe(self, audio, language="pt", sample_rate=None):
        r = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return r


# ── resampling in STTService ──────────────────────────────────────────────────


def test_transcribe_48khz_resamples_to_16khz_before_backend():
    """Quando sample_rate=48000, backend deve receber ~16000 amostras float32."""
    backend = CapturingBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    audio_48k = _pcm_bytes(48000)  # 1 s a 48 kHz
    svc.transcribe(audio_48k, sample_rate=48000)

    assert backend.received, "Backend nao foi chamado"
    received = backend.received[0]
    # Resultado deve ser ndarray float32 com ~16000 amostras (1 s a 16 kHz)
    assert hasattr(received, "dtype"), "Backend deve receber ndarray, nao bytes"
    assert abs(len(received) - 16000) <= 2


def test_transcribe_44100hz_resamples_correctly():
    """sample_rate=44100 deve produzir ~16000/44100 amostras."""
    import math
    backend = CapturingBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    n_in = 44100
    svc.transcribe(_pcm_bytes(n_in), sample_rate=44100)

    expected = round(n_in * 16000 / 44100)
    received_len = len(backend.received[0])
    assert abs(received_len - expected) <= 2


def test_transcribe_16khz_bytes_not_preprocessed():
    """sample_rate=16000 nao deve pre-processar audio — backend recebe os bytes originais."""
    backend = CapturingBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    audio = _pcm_bytes(8000)  # 0.5 s a 16 kHz
    svc.transcribe(audio, sample_rate=16000)

    assert backend.received[0] is audio  # mesmo objeto, sem conversao pelo STTService


def test_transcribe_resampling_happens_once_across_retries():
    """O resample deve ocorrer uma unica vez, antes do loop de retries."""
    backend = CapturingBackend(response="ok", confidence=0.3)
    backend._confidence = 0.3  # abaixo do limiar → vai retentar
    svc = STTService(backend=backend, confidence_threshold=0.8, max_retries=2)

    audio_48k = _pcm_bytes(48000)
    svc.transcribe(audio_48k, sample_rate=48000)

    # Todas as tentativas devem receber o mesmo ndarray resamplado
    assert len(backend.received) == 3  # 1 inicial + 2 retries
    first = backend.received[0]
    for subsequent in backend.received[1:]:
        assert subsequent is first, "Backend deve receber o mesmo objeto em todos os retries"


# ── warning behavior ──────────────────────────────────────────────────────────


def test_bytes_without_sample_rate_emits_warning(caplog):
    """Bytes sem sample_rate devem gerar WARNING."""
    backend = StubBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    with caplog.at_level(logging.WARNING, logger="assistente_voz.stt"):
        svc.transcribe(_pcm_bytes(16000))  # sem sample_rate

    assert any("sample_rate" in m for m in caplog.messages), (
        "WARNING deve mencionar sample_rate"
    )


def test_ndarray_without_sample_rate_no_warning(caplog):
    """ndarray sem sample_rate NAO deve gerar WARNING (assumido pre-processado)."""
    import numpy as np

    backend = StubBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    audio_arr = np.zeros(16000, dtype=np.float32)
    with caplog.at_level(logging.WARNING, logger="assistente_voz.stt"):
        svc.transcribe(audio_arr)  # sem sample_rate

    assert not any("sample_rate" in m for m in caplog.messages)


def test_bytes_with_sample_rate_16000_no_warning(caplog):
    """Bytes com sample_rate=16000 nao devem gerar WARNING."""
    backend = StubBackend()
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    with caplog.at_level(logging.WARNING, logger="assistente_voz.stt"):
        svc.transcribe(_pcm_bytes(16000), sample_rate=16000)

    assert not any("sample_rate" in m for m in caplog.messages)


# ── FasterWhisperBackend.transcribe com sample_rate ──────────────────────────


def test_faster_whisper_backend_uses_sample_rate_override():
    """FasterWhisperBackend.transcribe(audio, sample_rate=X) deve usar X para _to_float32."""
    from unittest.mock import MagicMock, patch
    from assistente_voz.stt import FasterWhisperBackend, clear_model_cache

    clear_model_cache()

    captured_samples: list = []

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), MagicMock())

    with patch("faster_whisper.WhisperModel", return_value=mock_model):
        backend = FasterWhisperBackend(model_size="tiny", input_sample_rate=16000)
        # Audio at 48 kHz — should be resampled to 16 kHz via sample_rate override
        audio_48k = _pcm_bytes(48000)
        backend.transcribe(audio_48k, sample_rate=48000)

    # WhisperModel.transcribe deve ter recebido um array com ~16000 amostras
    assert mock_model.transcribe.called
    samples_arg = mock_model.transcribe.call_args[0][0]
    assert abs(len(samples_arg) - 16000) <= 2

    clear_model_cache()


# ── protocolo e retrocompatibilidade ─────────────────────────────────────────


def test_stub_backend_satisfies_protocol():
    """StubBackend com nova assinatura deve satisfazer WhisperBackend."""
    assert isinstance(StubBackend(), WhisperBackend)


def test_capturing_backend_satisfies_protocol():
    assert isinstance(CapturingBackend(), WhisperBackend)


def test_existing_call_without_sample_rate_still_works():
    """Chamadores antigos que nao passam sample_rate continuam funcionando."""
    backend = StubBackend(responses=[("resultado", 0.9)])
    svc = STTService(backend=backend, confidence_threshold=0.5, max_retries=0)

    outcome = svc.transcribe(_pcm_bytes(16000))  # chamada legada

    assert outcome.status == TranscriptionStatus.OK
    assert outcome.result is not None
    assert outcome.result.text == "resultado"
