"""Issue 22 – Fallback Silencioso de Sample Rate no Windows.

Garante que:
- _to_float32 reamosta corretamente de qualquer taxa para 16 kHz.
- MicrophoneSource emite WARNING quando a taxa efetiva difere de 16 kHz.
- MicrophoneSource emite WARNING quando a taxa solicitada e rejeitada (fallback).
- MicrophoneSource emite INFO com a taxa efetiva em toda abertura bem-sucedida.
- Nenhum WARNING de mismatch quando o dispositivo aceita 16 kHz diretamente.
- _safe_transcribe entrega audio em 16 kHz ao backend independentemente da taxa capturada.
"""
from __future__ import annotations

import array
import logging
from unittest.mock import MagicMock, patch

import pytest

from assistente_voz.stt import _to_float32


# ── helpers ───────────────────────────────────────────────────────────────────


def _pcm_bytes(n_samples: int, value: int = 0) -> bytes:
    """Build 16-bit PCM bytes with constant amplitude."""
    return array.array("h", [value] * n_samples).tobytes()


class _FakeStream:
    """Minimal sounddevice.RawInputStream stand-in."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _patch_sd(accepted_rate: int):
    """Return a patch context for sounddevice.RawInputStream that only accepts *accepted_rate*."""

    def fake_raw_input_stream(samplerate, **kwargs):
        if samplerate != accepted_rate:
            raise OSError(f"PaError -9997: invalid sample rate {samplerate}")
        return _FakeStream()

    return patch("sounddevice.RawInputStream", side_effect=fake_raw_input_stream)


# ── _to_float32 resampling ────────────────────────────────────────────────────


def test_to_float32_resamples_44100_to_16000():
    """44100 Hz PCM deve produzir ~16000/44100 amostras apos resample."""
    n_in = 44100  # 1 s a 44100 Hz
    audio = _pcm_bytes(n_in)
    result = _to_float32(audio, sample_rate=44100)

    expected = round(n_in * 16000 / 44100)
    assert abs(len(result) - expected) <= 2, (
        f"Esperado ~{expected} amostras apos resample de 44100->16000, obtido {len(result)}"
    )


def test_to_float32_resamples_48000_to_16000():
    """48000 Hz PCM deve produzir exatamente 1/3 das amostras."""
    n_in = 48000  # 1 s a 48000 Hz
    audio = _pcm_bytes(n_in)
    result = _to_float32(audio, sample_rate=48000)

    expected = 16000
    assert abs(len(result) - expected) <= 2, (
        f"Esperado ~{expected} amostras apos resample de 48000->16000, obtido {len(result)}"
    )


def test_to_float32_passthrough_at_16000():
    """Audio ja em 16 kHz nao deve alterar a contagem de amostras."""
    n_in = 16000  # 1 s a 16 kHz
    audio = _pcm_bytes(n_in)
    result = _to_float32(audio, sample_rate=16000)

    assert len(result) == n_in, (
        f"Audio ja em 16 kHz nao deve ser resamplado; obtido {len(result)} != {n_in}"
    )


def test_to_float32_output_range():
    """Amostras de saida devem estar em [-1, 1]."""
    audio = _pcm_bytes(1000, value=16000)
    result = _to_float32(audio, sample_rate=44100)

    assert result.min() >= -1.0
    assert result.max() <= 1.0


# ── MicrophoneSource warning logs ─────────────────────────────────────────────


def test_warning_logged_when_fallback_rate_is_not_16000(caplog):
    """WARNING deve ser emitido quando o dispositivo rejeita 16 kHz e aceita outro rate."""
    from assistente_voz.audio_capture import MicrophoneSource

    src = MicrophoneSource(sample_rate=16000)
    with caplog.at_level(logging.WARNING, logger="assistente_voz.audio_capture"):
        with _patch_sd(accepted_rate=48000):
            src.open()
            src.close()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "Esperado pelo menos um WARNING, mas nenhum foi emitido"

    full_text = " ".join(r.message for r in warnings)
    assert "48000" in full_text, f"WARNING deve mencionar a taxa efetiva (48000); obtido: {full_text!r}"


def test_warning_logged_about_fallback_from_requested_rate(caplog):
    """WARNING deve mencionar que a taxa solicitada foi rejeitada."""
    from assistente_voz.audio_capture import MicrophoneSource

    src = MicrophoneSource(sample_rate=16000)
    with caplog.at_level(logging.WARNING, logger="assistente_voz.audio_capture"):
        with _patch_sd(accepted_rate=48000):
            src.open()
            src.close()

    full_text = " ".join(r.message for r in caplog.records if r.levelno == logging.WARNING)
    assert "16000" in full_text, (
        f"WARNING deve mencionar a taxa solicitada (16000); obtido: {full_text!r}"
    )


def test_info_logged_with_effective_sample_rate(caplog):
    """INFO deve registrar a taxa efetiva em toda abertura bem-sucedida."""
    from assistente_voz.audio_capture import MicrophoneSource

    src = MicrophoneSource(sample_rate=16000)
    with caplog.at_level(logging.INFO, logger="assistente_voz.audio_capture"):
        with _patch_sd(accepted_rate=16000):
            src.open()
            src.close()

    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("16000" in m for m in info_msgs), (
        f"INFO deve mencionar a taxa efetiva (16000); mensagens: {info_msgs}"
    )


def test_no_rate_mismatch_warning_when_device_accepts_16000(caplog):
    """Nenhum WARNING de mismatch STT deve ser emitido quando dispositivo aceita 16 kHz."""
    from assistente_voz.audio_capture import MicrophoneSource

    src = MicrophoneSource(sample_rate=16000)
    with caplog.at_level(logging.WARNING, logger="assistente_voz.audio_capture"):
        with _patch_sd(accepted_rate=16000):
            src.open()
            src.close()

    mismatch_warnings = [
        r.message
        for r in caplog.records
        if r.levelno == logging.WARNING and (
            "differ" in r.message.lower()
            or "resamp" in r.message.lower()
            or "whisper" in r.message.lower()
        )
    ]
    assert mismatch_warnings == [], (
        f"Nenhum WARNING de mismatch esperado quando dispositivo aceita 16 kHz; "
        f"obtido: {mismatch_warnings}"
    )


def test_effective_sample_rate_attribute_reflects_actual_rate():
    """sample_rate do source deve refletir a taxa efetivamente aberta."""
    from assistente_voz.audio_capture import MicrophoneSource

    src = MicrophoneSource(sample_rate=16000)
    with _patch_sd(accepted_rate=48000):
        src.open()
        effective = src.sample_rate
        src.close()

    assert effective == 48000, (
        f"sample_rate deve ser 48000 apos fallback; obtido {effective}"
    )


# ── _safe_transcribe entrega 16 kHz ao backend ────────────────────────────────


def test_safe_transcribe_resamples_to_16000_before_backend():
    """_safe_transcribe deve entregar ao backend audio ja em 16 kHz.

    Verifica indiretamente via comprimento do array: se o backend recebeu
    audio em 16 kHz, o numero de amostras deve corresponder a duracao * 16000.
    """
    from assistente_voz.live_caption import _safe_transcribe
    from assistente_voz.stt import FasterWhisperBackend

    received_samples: list = []

    class _CapturingBackend(FasterWhisperBackend):
        def transcribe(self, audio, language="pt"):
            received_samples.append(audio)
            return "ok", 0.9

    backend = _CapturingBackend.__new__(_CapturingBackend)
    backend._input_sample_rate = 16000
    backend._model = None

    # 1 segundo de audio a 48000 Hz (int16 PCM)
    n_samples_48k = 48000
    buf = _pcm_bytes(n_samples_48k)

    _safe_transcribe(backend, buf, sr=48000, language="pt")

    assert received_samples, "_safe_transcribe deve chamar backend.transcribe"
    arr = received_samples[0]
    expected_len = 16000  # 1 s a 16 kHz
    assert abs(len(arr) - expected_len) <= 2, (
        f"Backend deve receber ~{expected_len} amostras (16 kHz); obtido {len(arr)}"
    )
