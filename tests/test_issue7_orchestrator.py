"""Issue 7 - Orquestrador E2E Base."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from assistente_voz.audio_capture import AudioSegment
from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMResponse
from assistente_voz.orchestrator import VoiceOrchestrator
from assistente_voz.stt import STTOutcome, TranscriptionResult, TranscriptionStatus


class StubMetrics:
    def __init__(self) -> None:
        self.items: list[tuple[str, float]] = []

    def observe(self, name: str, value: float, tags=None) -> None:
        self.items.append((name, value))


class StubSTT:
    def __init__(self, outcome: STTOutcome | None = None, error: Exception | None = None) -> None:
        self._outcome = outcome
        self._error = error
        self.calls = 0

    def transcribe(self, audio: bytes) -> STTOutcome:
        self.calls += 1
        if self._error:
            raise self._error
        assert self._outcome is not None
        return self._outcome


class StubLLM:
    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls = 0
        self.applied = False

    def apply_profile(self, state: ConversationState) -> None:
        self.applied = True
        state.set_system("system test prompt")

    def complete(self, state: ConversationState) -> LLMResponse:
        self.calls += 1
        if self._error:
            raise self._error
        assert self._response is not None
        return self._response

    def stream(self, state: ConversationState, on_token) -> LLMResponse:
        return self.complete(state)


def _segment() -> AudioSegment:
    return AudioSegment(audio_bytes=b"abc", sample_rate=16000, channels=1, source="test")


def _ok_stt_outcome() -> STTOutcome:
    return STTOutcome(
        status=TranscriptionStatus.OK,
        result=TranscriptionResult(text="paciente com dor", confidence=0.91, language="pt"),
        attempts=1,
    )


def test_e2e_success_flow_updates_state_and_returns_response():
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(outcome=_ok_stt_outcome())
    llm = StubLLM(response=LLMResponse(text="Entendi, vamos continuar.", model="stub", latency_ms=12))

    orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics)
    result = orch.process_segment(_segment())

    assert result.ok is True
    assert result.transcription == "paciente com dor"
    assert result.assistant_response == "Entendi, vamos continuar."
    assert llm.calls == 1
    assert [m.role for m in state.messages()] == ["user", "assistant"]


def test_metrics_register_stt_llm_and_e2e_latencies():
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(outcome=_ok_stt_outcome())
    llm = StubLLM(response=LLMResponse(text="ok", model="stub", latency_ms=5))

    orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics)
    orch.process_segment(_segment())

    names = [name for (name, _) in metrics.items]
    assert "stt.latency_ms" in names
    assert "llm.latency_ms" in names
    assert "e2e.latency_ms" in names


def test_low_confidence_keeps_session_active_and_does_not_call_llm():
    low = STTOutcome(
        status=TranscriptionStatus.LOW_CONFIDENCE,
        result=TranscriptionResult(text="texto incerto", confidence=0.40, language="pt"),
        attempts=3,
    )
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(outcome=low)
    llm = StubLLM(response=LLMResponse(text="n/a", model="stub", latency_ms=1))

    orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics)
    result = orch.process_segment(_segment())

    assert result.ok is False
    assert result.error_code == "STT_LOW_CONFIDENCE"
    assert llm.calls == 0
    assert len(state.messages()) == 0


def test_stt_exception_is_fail_soft_and_returns_error_code():
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(error=RuntimeError("stt crash"))
    llm = StubLLM(response=LLMResponse(text="n/a", model="stub", latency_ms=1))

    orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics)
    result = orch.process_segment(_segment())

    assert result.ok is False
    assert result.error_code == "STT_EXCEPTION"
    assert llm.calls == 0


def test_llm_exception_is_fail_soft_and_preserves_transcription():
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(outcome=_ok_stt_outcome())
    llm = StubLLM(error=TimeoutError("llm timeout"))

    orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics)
    result = orch.process_segment(_segment())

    assert result.ok is False
    assert result.error_code == "LLM_EXCEPTION"
    assert result.transcription == "paciente com dor"


def test_logs_include_module_and_error_code_without_sensitive_payload(caplog):
    metrics = StubMetrics()
    state = ConversationState()
    stt = StubSTT(error=RuntimeError("boom"))
    llm = StubLLM(response=LLMResponse(text="n/a", model="stub", latency_ms=1))

    logger = logging.getLogger("assistente_voz.test.orchestrator")

    with caplog.at_level(logging.ERROR):
        orch = VoiceOrchestrator(stt=stt, llm=llm, state=state, metrics=metrics, logger=logger)
        orch.process_segment(_segment())

    joined = "\n".join(caplog.messages)
    assert "module=stt" in joined
    assert "code=STT_EXCEPTION" in joined
    assert "abc" not in joined
