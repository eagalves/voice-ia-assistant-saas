"""Issue 12 - TextUI Operacional com Correcoes de Sessao."""

import pytest

from assistente_voz.orchestrator import TurnResult
from assistente_voz.text_ui import TextUI


def _ok_result() -> TurnResult:
    return TurnResult(
        ok=True,
        transcription="paciente relata dor",
        assistant_response="Vamos detalhar melhor os sintomas.",
        stt_status="OK",
        stt_attempts=1,
        stt_confidence=0.9,
        stt_latency_ms=120,
        llm_latency_ms=240,
        e2e_latency_ms=400,
    )


def _low_conf_result() -> TurnResult:
    return TurnResult(
        ok=False,
        transcription="texto incerto",
        assistant_response=None,
        stt_status="LOW_CONFIDENCE",
        stt_attempts=3,
        stt_confidence=0.4,
        stt_latency_ms=120,
        llm_latency_ms=None,
        e2e_latency_ms=180,
        error_code="STT_LOW_CONFIDENCE",
    )


def test_realtime_snapshot_updates_transcription_and_response():
    ui = TextUI()
    ui.on_listening_started()
    ui.on_processing()
    ui.on_turn_result(_ok_result())

    snap = ui.render_snapshot()

    assert snap["status"] == "listening"
    assert snap["transcription"] == "paciente relata dor"
    assert snap["response"] == "Vamos detalhar melhor os sintomas."


def test_manual_fallback_requires_confirmation():
    ui = TextUI()
    ui.on_turn_result(_low_conf_result())

    with pytest.raises(ValueError):
        ui.submit_manual_fallback("texto manual", confirmed=False)


def test_manual_fallback_confirms_and_returns_to_session_flow():
    ui = TextUI()
    ui.on_turn_result(_low_conf_result())

    accepted = ui.submit_manual_fallback("texto manual confirmado", confirmed=True)
    snap = ui.render_snapshot()

    assert accepted == "texto manual confirmado"
    assert snap["fallback_pending"] is False
    assert snap["transcription"] == "texto manual confirmado"


def test_final_adjustments_are_applied_before_emission():
    ui = TextUI()
    ui.apply_final_adjustments(["corrigir role do segmento s1", "ajustar resumo final"])

    assert len(ui.state.final_notes) == 2
    assert "ajustar resumo final" in ui.state.final_notes
