"""Issue 31 — Correções de robustez no núcleo MVP (revisão de 2026-10-27).

AC1 — Red flags são detectados com acentuação pt-BR ("convulsão", "ideação suicida")
AC2 — Detecção por palavra inteira: "ardor" não dispara "dor"; termos do PRD cobertos
AC3 — Orquestrador repassa ``sample_rate`` do segmento ao STT (mic 44.1/48 kHz)
AC4 — ``LLMService.stream`` faz retry só em LLM_ERR_UNAVAILABLE e só antes do 1º token
AC5 — Falha lógica antes do 1º token não é retentada; falha após 1º token propaga
AC6 — ``STT_CONFIDENCE`` inválido/fora da faixa → warning + 0.55 (sem crash no boot)
AC7 — Pós-correção respeita limite de palavra ("dor"→"dose" não altera "ardor")
AC8 — ``FasterWhisperBackend.from_env`` lê os mesmos knobs do Voice CLI
"""
from __future__ import annotations

import json
import logging

import pytest

from assistente_voz.audio_capture import AudioSegment
from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMService, LLMTechnicalError
from assistente_voz.medical_rules import MedicalProfileEngine, detect_red_flags
from assistente_voz.orchestrator import VoiceOrchestrator
from assistente_voz.stt import (
    STTOutcome,
    STTService,
    TranscriptionResult,
    TranscriptionStatus,
    parse_confidence_env,
)
from assistente_voz.stt_correction import CorrectionOutcome, TranscriptCorrector


# ── AC1 / AC2 — red flags ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Teve uma convulsão ontem à noite", ["convulsao"]),
        ("Relata ideação suicida há semanas", ["ideacao suicida"]),
        ("Dor torácica irradiando para o braço esquerdo", ["dor toracica"]),
        ("Perda de consciência súbita", ["perda de consciencia"]),
        ("DISPNEIA aos esforços", ["dispneia"]),
    ],
)
def test_ac1_accented_terms_are_detected(text, expected):
    assert detect_red_flags(text) == expected


def test_ac2_whole_word_matching():
    assert detect_red_flags("Sente ardor no estômago e desconforto") == []
    assert detect_red_flags("desmaiou? não, só um desmaio leve") == ["desmaio"]
    assert detect_red_flags("") == []


def test_ac2_engine_uses_normalized_detection():
    engine = MedicalProfileEngine()
    record = engine.build_record({"queixa_principal": "Convulsão e sangramento intenso"})
    assert record.red_flags_detected == ["convulsao", "sangramento intenso"]
    assert "Sinais de alerta identificados" in record.sections["orientacao_final_de_seguranca"]


# ── AC3 — sample_rate propagation ────────────────────────────────────────────

class _RateCapturingSTT:
    def __init__(self) -> None:
        self.rates: list[int | None] = []

    def transcribe(self, audio, sample_rate=None) -> STTOutcome:
        self.rates.append(sample_rate)
        return STTOutcome(
            status=TranscriptionStatus.OK,
            result=TranscriptionResult(text="ok", confidence=0.9, language="pt"),
            attempts=1,
        )


class _EchoLLM:
    def apply_profile(self, state: ConversationState) -> None:
        state.set_system("sys")

    def stream(self, state: ConversationState, on_token):
        from assistente_voz.llm import LLMResponse

        return LLMResponse(text="resposta", model="m", latency_ms=1.0)

    def complete(self, state: ConversationState):
        return self.stream(state, lambda _: None)


def test_ac3_orchestrator_forwards_segment_sample_rate():
    stt = _RateCapturingSTT()
    orch = VoiceOrchestrator(stt=stt, llm=_EchoLLM(), state=ConversationState())
    seg = AudioSegment(audio_bytes=b"\x00\x01" * 100, sample_rate=48000, channels=1, source="test")
    result = orch.process_segment(seg)
    assert result.ok
    assert stt.rates == [48000]


# ── AC4 / AC5 — stream retry ─────────────────────────────────────────────────

class _FlakyStreamBackend:
    provider_name = "flaky"

    def __init__(self, failures_before_success: int, exc: Exception, fail_after_token: bool = False):
        self._remaining = failures_before_success
        self._exc = exc
        self._fail_after_token = fail_after_token
        self.calls = 0

    def complete(self, messages):
        return "completo", "m", self.provider_name

    def stream(self, messages):
        self.calls += 1
        if self._fail_after_token:
            yield "tok"
            raise self._exc
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        yield "a"
        yield "b"


def _svc(backend) -> LLMService:
    return LLMService(backend=backend, profile_data={}, max_retries=2, backoff_base_s=0.0, backoff_max_s=0.0)


def test_ac4_stream_retries_unavailable_before_first_token():
    backend = _FlakyStreamBackend(2, LLMTechnicalError("down"))
    tokens: list[str] = []
    resp = _svc(backend).stream(ConversationState(), on_token=tokens.append)
    assert backend.calls == 3
    assert tokens == ["a", "b"] and resp.text == "ab"


def test_ac4_stream_gives_up_after_retries():
    backend = _FlakyStreamBackend(5, LLMTechnicalError("down"))
    with pytest.raises(LLMTechnicalError):
        _svc(backend).stream(ConversationState(), on_token=lambda _: None)
    assert backend.calls == 3


def test_ac5_logical_error_not_retried():
    backend = _FlakyStreamBackend(1, ValueError("bad request"))
    with pytest.raises(ValueError):
        _svc(backend).stream(ConversationState(), on_token=lambda _: None)
    assert backend.calls == 1


def test_ac5_failure_after_first_token_propagates_without_retry():
    backend = _FlakyStreamBackend(0, LLMTechnicalError("down"), fail_after_token=True)
    tokens: list[str] = []
    with pytest.raises(LLMTechnicalError):
        _svc(backend).stream(ConversationState(), on_token=tokens.append)
    assert tokens == ["tok"] and backend.calls == 1


# ── AC6 — STT_CONFIDENCE tolerant parsing ────────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "abc", "0.1", "0.99"])
def test_ac6_invalid_confidence_falls_back_with_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="assistente_voz.stt"):
        assert parse_confidence_env(raw) == pytest.approx(0.55)
    assert any("STT_CONFIDENCE" in m for m in caplog.messages)


def test_ac6_valid_confidence_is_kept():
    assert parse_confidence_env("0.70") == pytest.approx(0.70)


def test_ac6_from_env_survives_invalid_values(monkeypatch):
    monkeypatch.setenv("STT_CONFIDENCE", "high")
    monkeypatch.setenv("STT_MAX_RETRIES", "many")
    monkeypatch.setenv("WHISPER_BEAM_SIZE", "wide")
    svc = STTService.from_env()  # must not raise; model is lazy
    assert svc._threshold == pytest.approx(0.55)  # noqa: SLF001
    assert svc._max_retries == 1  # noqa: SLF001


# ── AC7 — correction word boundaries ─────────────────────────────────────────

class _FixedBackend:
    provider_name = "fake"

    def __init__(self, edits: list[tuple[str, str]]) -> None:
        self._raw = json.dumps([{"de": a, "para": b} for a, b in edits])

    def complete(self, messages):
        return self._raw, "m", self.provider_name


def test_ac7_edit_does_not_rewrite_inside_words():
    corrector = TranscriptCorrector(backend=_FixedBackend([("dor", "dose")]))
    r = corrector.correct("sente ardor e dor aqui")
    assert r.outcome == CorrectionOutcome.APPLIED
    assert r.text == "sente ardor e dose aqui"


def test_ac7_edit_absent_as_whole_word_is_rejected():
    corrector = TranscriptCorrector(backend=_FixedBackend([("dor", "dose")]))
    r = corrector.correct("sente ardor")
    assert r.outcome == CorrectionOutcome.UNCHANGED
    assert r.text == "sente ardor"


# ── AC8 — backend factory ────────────────────────────────────────────────────

def test_ac8_backend_from_env_reads_whisper_knobs(monkeypatch):
    from assistente_voz.stt import FasterWhisperBackend

    monkeypatch.setenv("WHISPER_MODEL", "tiny")
    monkeypatch.setenv("WHISPER_BEAM_SIZE", "3")
    monkeypatch.setenv("WHISPER_HOTWORDS", "losartana")
    monkeypatch.setenv("WHISPER_TEMPERATURE", "0.0")
    backend = FasterWhisperBackend.from_env()
    assert backend._model_size == "tiny"  # noqa: SLF001
    assert backend._beam_size == 3  # noqa: SLF001
    assert backend._hotwords == "losartana"  # noqa: SLF001
    assert backend._temperature == 0.0  # noqa: SLF001
