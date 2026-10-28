"""Issue 27 - Orchestrator repassa tokens parciais para TextUI em tempo real.

Fecha o caminho arquitetural descrito na issue: o `VoiceOrchestrator` deve
repassar cada token recebido do LLM para a `TextUI`, que exibe a resposta
sendo gerada em tempo real (com indicador visual via `is_streaming`).
"""
from __future__ import annotations

from typing import Callable

from assistente_voz.audio_capture import AudioSegment
from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMResponse
from assistente_voz.orchestrator import VoiceOrchestrator
from assistente_voz.stt import STTOutcome, TranscriptionResult, TranscriptionStatus
from assistente_voz.text_ui import TextUI


class _StubMetrics:
    def __init__(self) -> None:
        self.items: list[tuple[str, float, dict | None]] = []

    def observe(self, name: str, value: float, tags=None) -> None:
        self.items.append((name, value, tags))


class _StubSTT:
    def transcribe(self, audio: bytes, sample_rate: int | None = None) -> STTOutcome:
        return STTOutcome(
            status=TranscriptionStatus.OK,
            result=TranscriptionResult(text="qual e o sintoma", confidence=0.93, language="pt"),
            attempts=1,
        )


class _StreamingLLM:
    """LLM stub that yields tokens one by one, so we can observe partial flow."""

    def __init__(self, tokens: list[str], ttft_ms: float = 42.0) -> None:
        self._tokens = tokens
        self._ttft_ms = ttft_ms
        self.tokens_seen_by_caller: list[str] = []

    def apply_profile(self, state: ConversationState) -> None:
        state.set_system("system")

    def complete(self, state: ConversationState) -> LLMResponse:
        return LLMResponse(text="".join(self._tokens), model="stub", latency_ms=10.0)

    def stream(self, state: ConversationState, on_token: Callable[[str], None]) -> LLMResponse:
        for tok in self._tokens:
            on_token(tok)
        return LLMResponse(
            text="".join(self._tokens),
            model="stub",
            latency_ms=10.0,
            time_to_first_token_ms=self._ttft_ms,
        )


class _NonStreamingLLM:
    """LLM stub whose stream() falls back to complete() (no tokens emitted)."""

    def apply_profile(self, state: ConversationState) -> None:
        state.set_system("system")

    def complete(self, state: ConversationState) -> LLMResponse:
        return LLMResponse(text="resposta inteira", model="stub-no-stream", latency_ms=10.0)

    def stream(self, state: ConversationState, on_token: Callable[[str], None]) -> LLMResponse:
        # Simulate LLMService falling back to complete() — no tokens emitted, ttft=0.
        return self.complete(state)


def _segment() -> AudioSegment:
    return AudioSegment(audio_bytes=b"abc", sample_rate=16000, channels=1, source="test")


def test_orchestrator_pipes_tokens_to_text_ui_in_realtime():
    """Cada token emitido pelo LLM deve aparecer no streaming_response da TextUI."""
    ui = TextUI()
    snapshots_during_stream: list[str] = []

    class _Recorder(TextUI):
        def on_token(self, token: str) -> None:
            super().on_token(token)
            snapshots_during_stream.append(self.state.streaming_response)

    ui = _Recorder()
    llm = _StreamingLLM(tokens=["Vamos", " investigar", " o sintoma."])
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=llm,
        state=ConversationState(),
        metrics=_StubMetrics(),
        stream_observer=ui,
    )

    result = orch.process_segment(_segment())

    assert result.ok is True
    assert snapshots_during_stream == [
        "Vamos",
        "Vamos investigar",
        "Vamos investigar o sintoma.",
    ]
    # After stream ends, streaming flag is cleared and accumulated text matches.
    assert ui.state.is_streaming is False
    assert ui.state.streaming_response == "Vamos investigar o sintoma."
    # Caller commits the final result into last_response.
    ui.on_turn_result(result)
    assert ui.state.last_response == "Vamos investigar o sintoma."


def test_text_ui_signals_streaming_indicator_during_stream():
    """is_streaming deve estar True durante o stream e False ao final."""
    state_during_token: dict[str, bool] = {}

    class _Probe(TextUI):
        def on_token(self, token: str) -> None:
            super().on_token(token)
            state_during_token["is_streaming"] = self.state.is_streaming
            state_during_token["status"] = self.state.status

    ui = _Probe()
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_StreamingLLM(tokens=["a", "b"]),
        state=ConversationState(),
        metrics=_StubMetrics(),
        stream_observer=ui,
    )
    orch.process_segment(_segment())

    assert state_during_token["is_streaming"] is True
    assert state_during_token["status"] == "streaming"
    assert ui.state.is_streaming is False


def test_streamed_text_matches_final_response():
    """Conteudo acumulado em streaming deve ser identico ao assistant_response final."""
    ui = TextUI()
    tokens = ["Entendi", ",", " vamos", " continuar."]
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_StreamingLLM(tokens=tokens),
        state=ConversationState(),
        metrics=_StubMetrics(),
        stream_observer=ui,
    )
    result = orch.process_segment(_segment())
    ui.on_turn_result(result)

    assert ui.state.last_response == "".join(tokens)
    assert result.assistant_response == "".join(tokens)


def test_ttft_metric_emitted_with_streaming_tag():
    """Quando ha streaming real, a metrica ttft deve ser emitida com tag mode=streaming."""
    metrics = _StubMetrics()
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_StreamingLLM(tokens=["a"], ttft_ms=33.5),
        state=ConversationState(),
        metrics=metrics,
    )
    orch.process_segment(_segment())

    ttft_calls = [item for item in metrics.items if item[0] == "llm.ttft_ms"]
    assert len(ttft_calls) == 1
    name, value, tags = ttft_calls[0]
    assert value == 33.5
    assert tags == {"mode": "streaming"}


def test_ttft_metric_emitted_with_fallback_tag_when_no_streaming():
    """Quando o backend nao suporta streaming (TTFT=0), tag deve ser mode=fallback."""
    metrics = _StubMetrics()
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_NonStreamingLLM(),
        state=ConversationState(),
        metrics=metrics,
    )
    orch.process_segment(_segment())

    ttft_calls = [item for item in metrics.items if item[0] == "llm.ttft_ms"]
    assert len(ttft_calls) == 1
    name, value, tags = ttft_calls[0]
    assert value == 0.0
    assert tags == {"mode": "fallback"}


def test_observer_stream_end_called_even_on_llm_error():
    """on_stream_end deve ser chamado mesmo quando o LLM falha durante o stream."""
    calls: list[str] = []

    class _BoomLLM:
        def apply_profile(self, state: ConversationState) -> None:
            state.set_system("system")

        def stream(self, state: ConversationState, on_token: Callable[[str], None]) -> LLMResponse:
            raise RuntimeError("LLM boom")

        def complete(self, state: ConversationState) -> LLMResponse:
            raise RuntimeError("LLM boom")

    class _TrackingUI(TextUI):
        def on_stream_start(self) -> None:
            calls.append("start")
            super().on_stream_start()

        def on_stream_end(self) -> None:
            calls.append("end")
            super().on_stream_end()

    ui = _TrackingUI()
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_BoomLLM(),
        state=ConversationState(),
        metrics=_StubMetrics(),
        stream_observer=ui,
    )
    result = orch.process_segment(_segment())

    assert result.ok is False
    assert result.error_code == "LLM_EXCEPTION"
    assert calls == ["start", "end"]
    assert ui.state.is_streaming is False


def test_orchestrator_works_without_stream_observer():
    """Sem observer, o orchestrator deve funcionar normalmente (compat)."""
    orch = VoiceOrchestrator(
        stt=_StubSTT(),
        llm=_StreamingLLM(tokens=["a", "b"]),
        state=ConversationState(),
        metrics=_StubMetrics(),
    )
    result = orch.process_segment(_segment())
    assert result.ok is True
    assert result.assistant_response == "ab"
