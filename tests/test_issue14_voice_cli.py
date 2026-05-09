"""Issue 14 - Voice CLI with LLM wired back in."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from assistente_voz.conversation_state import ConversationState
from assistente_voz.live_caption import CaptionEvent
from assistente_voz.voice_cli import run_text_mode, run_voice_mode


@dataclass(frozen=True)
class _FakeLLMResponse:
    text: str
    model: str
    latency_ms: float
    time_to_first_token_ms: float = 0.0


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, state: ConversationState) -> _FakeLLMResponse:
        self.calls += 1
        last_user = state.messages()[-1].content
        return _FakeLLMResponse(
            text=f"Resposta para: {last_user}",
            model="fake-model",
            latency_ms=7.0,
        )


class _FakeMicrophoneSource:
    def __init__(self, sample_rate: int, chunk_frames: int, device, debug: bool) -> None:
        self.sample_rate = sample_rate

    @staticmethod
    def list_devices() -> list[dict]:
        return [{"index": 0, "name": "Fake Mic", "default_samplerate": 16000, "max_input_channels": 1}]


class _FakeStreamingTranscriber:
    def __init__(self, **kwargs) -> None:
        self._on_event = kwargs["on_event"]

    def start(self) -> None:
        self._on_event(CaptionEvent(text="fala final um", is_final=True, confidence=0.91, audio_ms=1800))
        self._on_event(CaptionEvent(text="parcial", is_final=False, confidence=0.8, audio_ms=700))
        self._on_event(CaptionEvent(text="fala final dois", is_final=True, confidence=0.89, audio_ms=1400))

    def stop(self, timeout: float = 5.0) -> None:  # noqa: ARG002
        return None


class _FakeStreamingTranscriberLowConf:
    def __init__(self, **kwargs) -> None:
        self._on_event = kwargs["on_event"]

    def start(self) -> None:
        self._on_event(CaptionEvent(text="fala duvidosa", is_final=True, confidence=0.20, audio_ms=1300))

    def stop(self, timeout: float = 5.0) -> None:  # noqa: ARG002
        return None


def test_text_mode_calls_llm_and_prints_response(monkeypatch, capsys):
    fake_llm = _FakeLLM()
    state = ConversationState()

    monkeypatch.setattr(
        "assistente_voz.voice_cli._build_llm_runtime",
        lambda: (fake_llm, state),
    )

    code = run_text_mode("teste rapido")
    out = capsys.readouterr().out

    assert code == 0
    assert fake_llm.calls == 1
    assert "[assistant model=fake-model" in out
    assert "Resposta para: teste rapido" in out


def test_voice_mode_calls_llm_for_each_final_caption(monkeypatch, capsys):
    fake_llm = _FakeLLM()
    state = ConversationState()

    monkeypatch.setattr("assistente_voz.voice_cli._build_llm_runtime", lambda: (fake_llm, state))
    monkeypatch.setattr("assistente_voz.voice_cli.MicrophoneSource", _FakeMicrophoneSource)
    monkeypatch.setattr("assistente_voz.voice_cli._make_final_backend", lambda debug: object())
    monkeypatch.setattr("assistente_voz.voice_cli._make_partial_backend", lambda debug: object())
    monkeypatch.setattr("assistente_voz.voice_cli._warmup", lambda backend, label, debug: None)
    monkeypatch.setattr("assistente_voz.voice_cli.StreamingTranscriber", _FakeStreamingTranscriber)

    args = argparse.Namespace(
        duration=0.01,
        no_tts=True,
        debug_audio=False,
        list_devices=False,
        input_device=None,
        live_caption=True,
        caption_window=8.0,
        text=None,
        vad_rms=250.0,
        partial_interval_ms=700,
        hang_ms=1200,
        preroll_ms=400,
        min_final_conf=0.55,
    )

    code = run_voice_mode(args)
    out = capsys.readouterr().out

    assert code == 0
    assert fake_llm.calls == 2
    assert "Resposta para: fala final um" in out
    assert "Resposta para: fala final dois" in out


def test_voice_mode_skips_low_confidence_final_before_llm(monkeypatch, capsys):
    fake_llm = _FakeLLM()
    state = ConversationState()

    monkeypatch.setattr("assistente_voz.voice_cli._build_llm_runtime", lambda: (fake_llm, state))
    monkeypatch.setattr("assistente_voz.voice_cli.MicrophoneSource", _FakeMicrophoneSource)
    monkeypatch.setattr("assistente_voz.voice_cli._make_final_backend", lambda debug: object())
    monkeypatch.setattr("assistente_voz.voice_cli._warmup", lambda backend, label, debug: None)
    monkeypatch.setattr("assistente_voz.voice_cli.StreamingTranscriber", _FakeStreamingTranscriberLowConf)

    args = argparse.Namespace(
        duration=0.01,
        no_tts=True,
        debug_audio=False,
        list_devices=False,
        input_device=None,
        live_caption=False,
        caption_window=8.0,
        text=None,
        vad_rms=250.0,
        partial_interval_ms=700,
        hang_ms=1200,
        preroll_ms=400,
        min_final_conf=0.55,
    )

    code = run_voice_mode(args)
    _ = capsys.readouterr().out

    assert code == 0
    assert fake_llm.calls == 0
