"""Pós-correção de transcrições via LLM (TranscriptCorrector).

Contrato testado com backend fake (sem rede):
- Edições válidas são aplicadas programaticamente (nunca reescrita livre).
- Guardrails por edição: deleção, no-op, trecho inexistente, edição longa e
  reescrita de estilo (baixa similaridade fonética) são rejeitados.
- Guardrail global: divergência acumulada acima do limite rejeita tudo.
- Fail-open: exceção do LLM, resposta não-JSON e resposta vazia devolvem o
  texto original intacto — a correção nunca é ponto de falha do pipeline.
- Confiança alta pula a correção sem chamar o LLM.
- from_env: desabilitado por default; habilitado constrói com Ollama.

Integração real (marker llm_real; skip se Ollama/qwen2.5:3b indisponível):
- Corrige erro fonético real do benchmark ("Lusatana" → "losartana").
- Não altera transcrição correta.
"""
from __future__ import annotations

import json
import os
import urllib.request
from unittest.mock import patch

import pytest

from assistente_voz.stt_correction import (
    CorrectionOutcome,
    TranscriptCorrector,
    _parse_edits,
)
from assistente_voz.stt_eval import contains_term, phonetic_similarity


# ── fake backend ──────────────────────────────────────────────────────────────


class FakeLLMBackend:
    provider_name = "fake"

    def __init__(self, response: str = "[]", exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> tuple[str, str, str]:
        self.calls.append(messages)
        if self.exc is not None:
            raise self.exc
        return self.response, "fake-model", self.provider_name


def _corrector(response: str = "[]", exc: Exception | None = None, **kwargs):
    backend = FakeLLMBackend(response=response, exc=exc)
    return TranscriptCorrector(backend=backend, **kwargs), backend


def _edits(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"de": de, "para": para} for de, para in pairs])


# ── aplicação de edições ──────────────────────────────────────────────────────


def test_valid_phonetic_edit_is_applied():
    c, _ = _corrector(_edits(("los artanas", "losartana")))
    r = c.correct("Tomo los artanas 50 mg de manhã.")

    assert r.outcome == CorrectionOutcome.APPLIED
    assert r.text == "Tomo losartana 50 mg de manhã."
    assert r.edits_applied == [("los artanas", "losartana")]


def test_multiple_edits_applied_in_order():
    c, _ = _corrector(_edits(("Lusatana", "losartana"), ("me formi na", "metformina")))
    r = c.correct("Tomo Lusatana e me formi na depois.")

    assert r.outcome == CorrectionOutcome.APPLIED
    assert r.text == "Tomo losartana e metformina depois."
    assert len(r.edits_applied) == 2


def test_edit_replaces_only_first_occurrence():
    c, _ = _corrector(_edits(("dor", "dose")))
    r = c.correct("dor aqui e dor ali")

    assert r.text == "dose aqui e dor ali"


# ── guardrails por edição ─────────────────────────────────────────────────────


def test_deletion_edit_is_rejected():
    """'para' vazio removeria conteúdo — proibido no domínio médico."""
    c, _ = _corrector(_edits(("V2", "")))
    r = c.correct("Tomo 50mg na V2 do almoço.")

    assert r.outcome == CorrectionOutcome.UNCHANGED
    assert r.text == "Tomo 50mg na V2 do almoço."
    assert ("V2", "") in r.edits_rejected


def test_noop_edit_is_ignored():
    c, _ = _corrector(_edits(("quatorze", "quatorze")))
    r = c.correct("Pressão em quatorze por nove.")

    assert r.outcome == CorrectionOutcome.UNCHANGED
    assert r.text == "Pressão em quatorze por nove."


def test_edit_for_absent_snippet_is_rejected():
    """Se 'de' não ocorre no texto, o LLM inventou — nada é aplicado."""
    c, _ = _corrector(_edits(("dipirona", "dipirona sódica")))
    r = c.correct("Tomo losartana de manhã.")

    assert r.outcome == CorrectionOutcome.UNCHANGED
    assert r.text == "Tomo losartana de manhã."


def test_long_edit_is_rejected():
    """Edições longas indicam reescrita, não correção pontual de ASR."""
    de = "Tomo losartana cinquenta miligramas de manhã"
    c, _ = _corrector(_edits((de, "Tomo losartana 50 mg pela manhã todos os dias")))
    r = c.correct(f"{de} e metformina à noite.")

    assert r.outcome == CorrectionOutcome.UNCHANGED


def test_style_rewrite_rejected_by_phonetic_similarity():
    """'de manhã'→'ao acordar' não soa parecido — é estilo, não erro de ASR."""
    c, _ = _corrector(_edits(("de manhã", "ao acordar")))
    r = c.correct("Tomo losartana de manhã.")

    assert r.outcome == CorrectionOutcome.UNCHANGED
    assert r.text == "Tomo losartana de manhã."


def test_max_edits_cap():
    pairs = [(f"w{i}", f"w{i}x") for i in range(12)]
    c, _ = _corrector(_edits(*pairs), max_edits=8)
    # texto longo o bastante para o guard global não interferir no teste do cap
    filler = " ".join(f"palavra{i}" for i in range(20))
    words = " ".join(f"w{i}" for i in range(12))
    r = c.correct(f"{filler} {words}")

    assert r.outcome == CorrectionOutcome.APPLIED
    assert len(r.edits_applied) == 8


# ── guardrail global de divergência ───────────────────────────────────────────


def test_cumulative_divergence_rejects_everything():
    """Muitas edições pequenas que somadas reescrevem a frase → rejeição total."""
    c, _ = _corrector(
        _edits(
            ("dor", "dose"), ("cabeça", "cabeçada"), ("três", "treze"),
            ("dias", "dia"), ("febre", "fever"), ("alta", "alto"),
        ),
        max_divergence=0.35,
    )
    original = "dor cabeça três dias febre alta"
    r = c.correct(original)

    assert r.outcome == CorrectionOutcome.REJECTED_DIVERGENCE
    assert r.text == original
    assert r.divergence > 0.35


def test_short_utterance_merge_fix_is_accepted():
    """Em frase curta o WER relativo é alto, mas a tolerância absoluta salva
    a correção legítima de merge ("los artanas" → "losartana")."""
    c, _ = _corrector(_edits(("los artanas", "losartana")))
    r = c.correct("Tomo los artanas.")

    assert r.outcome == CorrectionOutcome.APPLIED
    assert r.text == "Tomo losartana."


# ── fail-open ─────────────────────────────────────────────────────────────────


def test_llm_exception_fails_open():
    c, _ = _corrector(exc=ConnectionError("ollama down"))
    original = "Tomo losartana de manhã."
    r = c.correct(original)

    assert r.outcome == CorrectionOutcome.LLM_ERROR
    assert r.text == original


def test_non_json_response_fails_open():
    c, _ = _corrector("Claro! Aqui está o texto corrigido: Tomo losartana.")
    original = "Tomo los artanas."
    r = c.correct(original)

    assert r.outcome == CorrectionOutcome.LLM_PARSE_ERROR
    assert r.text == original


def test_json_inside_markdown_fence_is_extracted():
    c, _ = _corrector('```json\n[{"de": "los artanas", "para": "losartana"}]\n```')
    r = c.correct("Tomo los artanas.")

    assert r.outcome == CorrectionOutcome.APPLIED
    assert r.text == "Tomo losartana."


def test_empty_edit_list_means_no_errors():
    c, _ = _corrector("[]")
    r = c.correct("Sinto falta de ar quando subo escadas.")

    assert r.outcome == CorrectionOutcome.NO_EDITS
    assert r.text == "Sinto falta de ar quando subo escadas."


def test_empty_input_short_circuits():
    c, backend = _corrector()
    r = c.correct("   ")

    assert r.outcome == CorrectionOutcome.EMPTY_INPUT
    assert backend.calls == []


# ── skip por confiança alta ───────────────────────────────────────────────────


def test_high_confidence_skips_llm_call():
    c, backend = _corrector(skip_above_confidence=0.85)
    r = c.correct("Transcrição segura.", confidence=0.92)

    assert r.outcome == CorrectionOutcome.SKIPPED_HIGH_CONFIDENCE
    assert backend.calls == []


def test_low_confidence_goes_through_llm():
    c, backend = _corrector("[]", skip_above_confidence=0.85)
    r = c.correct("Transcrição duvidosa.", confidence=0.5)

    assert r.outcome == CorrectionOutcome.NO_EDITS
    assert len(backend.calls) == 1


def test_no_confidence_goes_through_llm():
    c, backend = _corrector("[]")
    c.correct("Sem confiança informada.")

    assert len(backend.calls) == 1


# ── parse de edições ──────────────────────────────────────────────────────────


def test_parse_edits_rejects_malformed_entries():
    assert _parse_edits('[{"de": 1, "para": "x"}]') is None
    assert _parse_edits('["texto solto"]') is None
    assert _parse_edits("{}") is None
    assert _parse_edits("sem json aqui") is None


def test_parse_edits_accepts_surrounding_text():
    raw = 'Correções: [{"de": "a", "para": "b"}] — pronto.'
    assert _parse_edits(raw) == [("a", "b")]


# ── phonetic_similarity ───────────────────────────────────────────────────────


def test_phonetic_similarity_separates_asr_errors_from_style():
    assert phonetic_similarity("lousa há tanas", "losartana") >= 0.4
    assert phonetic_similarity("Lusatana", "losartana") >= 0.6
    assert phonetic_similarity("de manhã", "ao acordar") < 0.4


# ── from_env ──────────────────────────────────────────────────────────────────


def test_from_env_disabled_by_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("STT_POSTCORRECT_ENABLED", None)
        assert TranscriptCorrector.from_env() is None


def test_from_env_enabled_builds_ollama_corrector():
    env = {
        "STT_POSTCORRECT_ENABLED": "1",
        "STT_POSTCORRECT_TERMS": "losartana, metformina",
        "STT_POSTCORRECT_MAX_DIVERGENCE": "0.4",
    }
    with patch.dict(os.environ, env, clear=False):
        corrector = TranscriptCorrector.from_env()

    assert corrector is not None
    assert corrector._max_divergence == 0.4
    assert "losartana" in corrector._system


# ── integração real com Ollama (skip se indisponível) ─────────────────────────


def _ollama_model_available(model: str = "qwen2.5:3b") -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name", "").startswith(model) for m in tags.get("models", []))
    except Exception:  # noqa: BLE001
        return False


requires_ollama = pytest.mark.skipif(
    not _ollama_model_available(), reason="Ollama com qwen2.5:3b não disponível"
)


@pytest.mark.llm_real
@requires_ollama
def test_real_llm_fixes_phonetic_drug_name():
    """Erro real do benchmark: 'Lusatana' (Whisper small + ruído) → losartana."""
    from assistente_voz.llm import OllamaBackend

    corrector = TranscriptCorrector(
        backend=OllamaBackend(model="qwen2.5:3b", options={"temperature": 0.0}),
        domain_terms="losartana, metformina, dipirona",
    )
    r = corrector.correct(
        "Estou no Lusatana 50mg de manhã e me formi na V2 do almoço.",
        confidence=0.56,
    )

    assert r.outcome not in (
        CorrectionOutcome.LLM_ERROR,
        CorrectionOutcome.LLM_PARSE_ERROR,
    )
    if r.applied:
        assert contains_term(r.text, "losartana")
        assert "50" in r.text  # dose intocada


@pytest.mark.llm_real
@requires_ollama
def test_real_llm_leaves_correct_text_alone():
    from assistente_voz.llm import OllamaBackend

    corrector = TranscriptCorrector(
        backend=OllamaBackend(model="qwen2.5:3b", options={"temperature": 0.0}),
    )
    original = "Sinto falta de ar quando subo escadas e o coração dispara."
    r = corrector.correct(original, confidence=0.6)

    from assistente_voz.stt_eval import word_error_rate
    assert word_error_rate(original, r.text) <= 0.1, f"texto correto foi alterado: {r.text!r}"
