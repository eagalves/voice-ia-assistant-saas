"""Pós-correção de transcrições STT via LLM — corrige erros pelo contexto.

O Whisper erra de forma característica: palavras trocadas por similaridade
fonética ("lousa a tanas" → "losartana"), segmentação errada e termos técnicos
grafados errado. Um LLM lendo a frase inteira detecta isso pelo contexto.

Design: **lista de edições, nunca reescrita.** Pedir ao LLM o texto reescrito
convida paráfrase (modelos pequenos trocam "depois do" por "após o" mesmo
instruídos a não fazê-lo). Em vez disso o LLM devolve um array JSON de
correções ``[{"de": "trecho errado", "para": "trecho corrigido"}]`` e NÓS
aplicamos as edições programaticamente. Paráfrase estrutural fica impossível:
só substituímos trechos que existem literalmente na transcrição.

Guardrails por edição (domínio médico — fidelidade acima de fluência):
- ``de`` precisa ocorrer exatamente no texto original;
- ``para`` vazio é rejeitado (o LLM não pode apagar conteúdo);
- edições no-op são ignoradas;
- no máximo ``max_edits`` edições, cada uma com até ``max_edit_words`` palavras
  (erros de ASR são locais; edições longas indicam reescrita);
- cada edição precisa soar parecida com o original (similaridade fonética
  ≥ ``min_edit_similarity``): "lousa há tanas"→"losartana" passa,
  "de manhã"→"ao acordar" é reescrita e cai.

Guardrails no resultado final:
- WER(original, corrigido) ≤ ``max_divergence`` — acima disso é paráfrase;
- fail-open: qualquer falha de LLM/parse devolve o original intacto.

Transcrições com confiança alta podem pular a correção
(``skip_above_confidence``) — onde o STT está seguro, mexer só adiciona risco.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from assistente_voz.llm import LLMBackend, build_backend_from_env
from assistente_voz.stt_eval import (
    normalize_pt_text,
    phonetic_similarity,
    word_edit_distance,
)

_logger = logging.getLogger(__name__)


class CorrectionOutcome(Enum):
    APPLIED = "CORRECTION_APPLIED"              # ≥1 edição aceita e aplicada
    NO_EDITS = "CORRECTION_NO_EDITS"            # LLM não encontrou erros ([])
    UNCHANGED = "CORRECTION_UNCHANGED"          # havia edições, nenhuma passou nas regras
    REJECTED_DIVERGENCE = "CORRECTION_REJECTED_DIVERGENCE"  # resultado mudou demais
    LLM_ERROR = "CORRECTION_LLM_ERROR"          # backend falhou → fail-open
    LLM_PARSE_ERROR = "CORRECTION_LLM_PARSE_ERROR"  # resposta não é JSON válido
    EMPTY_INPUT = "CORRECTION_EMPTY_INPUT"      # nada a corrigir
    SKIPPED_HIGH_CONFIDENCE = "CORRECTION_SKIPPED_HIGH_CONFIDENCE"


@dataclass(frozen=True)
class CorrectionResult:
    text: str                 # texto final (corrigido ou original, conforme outcome)
    original: str
    outcome: CorrectionOutcome
    edits_applied: list[tuple[str, str]] = field(default_factory=list)
    edits_rejected: list[tuple[str, str]] = field(default_factory=list)
    divergence: float = 0.0   # WER entre original e texto final
    latency_ms: float = 0.0

    @property
    def applied(self) -> bool:
        return self.outcome == CorrectionOutcome.APPLIED


_SYSTEM_PROMPT = (
    "Você revisa transcrições de reconhecimento de voz (ASR) em português do "
    "Brasil, no contexto de {context}. Identifique APENAS palavras transcritas "
    "erradas: trocas por similaridade fonética, nomes de medicamentos e termos "
    "técnicos grafados errado, segmentação incorreta de palavras.\n"
    'Responda SOMENTE com um array JSON no formato '
    '[{{"de": "trecho exato errado", "para": "trecho corrigido"}}].\n'
    "Regras obrigatórias:\n"
    "1. O campo 'de' deve copiar o trecho EXATAMENTE como aparece na transcrição.\n"
    "2. Não corrija estilo nem troque palavras corretas por sinônimos.\n"
    "3. Não remova conteúdo: 'para' nunca pode ser vazio.\n"
    "4. Preserve números, doses e unidades exatamente como transcritos.\n"
    "5. Se não houver erros, responda [].{terms}"
)

# Few-shot mantém modelos locais pequenos (qwen2.5:3b) no formato certo:
# uma correção fonética real e um caso sem erros.
_FEW_SHOT: list[dict] = [
    {"role": "user", "content": "o paciente toma lousa há tanas cinquenta miligramas por dia"},
    {"role": "assistant", "content": '[{"de": "lousa há tanas", "para": "losartana"}]'},
    {"role": "user", "content": "Sinto falta de ar quando subo escadas."},
    {"role": "assistant", "content": "[]"},
]

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def _find_snippet(text: str, snippet: str) -> int:
    """Index of *snippet* in *text* respecting word boundaries; -1 if absent.

    A plain substring search would let an edit ``dor → dose`` rewrite the
    inside of "ardor". Boundaries are only enforced on the sides of the
    snippet that start/end with a word character (so "50mg" still matches
    inside "50mg.").
    """
    pattern = re.escape(snippet)
    if snippet[:1].isalnum():
        pattern = r"(?<!\w)" + pattern
    if snippet[-1:].isalnum():
        pattern = pattern + r"(?!\w)"
    match = re.search(pattern, text)
    return match.start() if match else -1


def _parse_edits(raw: str) -> list[tuple[str, str]] | None:
    """Extrai a lista de edições (de, para) da resposta do LLM; None se inválida."""
    match = _JSON_ARRAY.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    edits: list[tuple[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            return None
        de, para = entry.get("de"), entry.get("para")
        if not isinstance(de, str) or not isinstance(para, str):
            return None
        edits.append((de, para))
    return edits


class TranscriptCorrector:
    """Aplica correções pontuais sugeridas por um LLM, sob guardrails.

    ``max_divergence`` default 0.35: como as edições já são locais e sem
    deleção, o resultado legítimo fica perto do original — divergência alta
    significa que muitas edições de estilo passaram e é melhor rejeitar tudo.
    Em frases curtas a razão engana ("Tomo los artanas." → "Tomo losartana."
    tem WER 0.67 legítimo), então o limite efetivo em palavras é
    ``max(min_word_allowance, max_divergence × nº de palavras)``.
    """

    def __init__(
        self,
        backend: LLMBackend,
        *,
        context: str = "uma consulta médica",
        domain_terms: str = "",
        max_divergence: float = 0.35,
        max_edits: int = 8,
        max_edit_words: int = 4,
        min_edit_similarity: float = 0.40,
        min_word_allowance: int = 4,
        skip_above_confidence: float | None = 0.85,
    ) -> None:
        self._backend = backend
        self._max_divergence = max_divergence
        self._max_edits = max_edits
        self._max_edit_words = max_edit_words
        self._min_edit_similarity = min_edit_similarity
        self._min_word_allowance = min_word_allowance
        self._skip_above = skip_above_confidence
        terms = (
            f"\nTermos prováveis neste contexto: {domain_terms.strip()}."
            if domain_terms.strip()
            else ""
        )
        self._system = _SYSTEM_PROMPT.format(context=context, terms=terms)

    # ── edit application ──────────────────────────────────────────────────────

    def _apply_edits(
        self, original: str, edits: list[tuple[str, str]]
    ) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
        """Aplica cada edição que passa nas regras; retorna (texto, aceitas, rejeitadas)."""
        text = original
        applied: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []
        for de, para in edits[: self._max_edits]:
            de, para = de.strip(), para.strip()
            position = _find_snippet(text, de) if de else -1
            if (
                not de
                or not para                       # deleção proibida
                or de == para                     # no-op
                or len(de.split()) > self._max_edit_words
                or len(para.split()) > self._max_edit_words
                or position < 0                   # trecho não existe → LLM inventou
                # correções reais de ASR soam parecido com o original;
                # reescritas de estilo ("de manhã"→"ao acordar") não.
                or phonetic_similarity(de, para) < self._min_edit_similarity
            ):
                rejected.append((de, para))
                continue
            text = text[:position] + para + text[position + len(de):]
            applied.append((de, para))
        return text, applied, rejected

    # ── public API ────────────────────────────────────────────────────────────

    def correct(self, text: str, confidence: float | None = None) -> CorrectionResult:
        """Retorna o texto corrigido, ou o original quando não for seguro trocar."""
        original = text.strip()
        if not original:
            return CorrectionResult("", text, CorrectionOutcome.EMPTY_INPUT)
        if (
            self._skip_above is not None
            and confidence is not None
            and confidence >= self._skip_above
        ):
            return CorrectionResult(
                original, original, CorrectionOutcome.SKIPPED_HIGH_CONFIDENCE
            )

        messages = [
            {"role": "system", "content": self._system},
            *_FEW_SHOT,
            {"role": "user", "content": original},
        ]

        t0 = time.perf_counter()
        try:
            raw, _model, _provider = self._backend.complete(messages)
        except Exception as exc:  # noqa: BLE001 — fail-open por design
            _logger.warning(
                "TranscriptCorrector: LLM falhou (%s) — mantendo texto original",
                type(exc).__name__,
            )
            return CorrectionResult(
                original, original, CorrectionOutcome.LLM_ERROR,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        latency_ms = (time.perf_counter() - t0) * 1000

        edits = _parse_edits(raw)
        if edits is None:
            _logger.info("TranscriptCorrector: resposta não parseável: %r", raw[:200])
            return CorrectionResult(
                original, original, CorrectionOutcome.LLM_PARSE_ERROR,
                latency_ms=latency_ms,
            )
        if not edits:
            return CorrectionResult(
                original, original, CorrectionOutcome.NO_EDITS, latency_ms=latency_ms
            )

        corrected, applied, rejected = self._apply_edits(original, edits)
        if not applied:
            return CorrectionResult(
                original, original, CorrectionOutcome.UNCHANGED,
                edits_rejected=rejected, latency_ms=latency_ms,
            )

        ref_words = len(normalize_pt_text(original).split())
        distance = word_edit_distance(original, corrected)
        divergence = distance / ref_words if ref_words else 0.0
        allowed_words = max(self._min_word_allowance, self._max_divergence * ref_words)
        if distance > allowed_words:
            _logger.info(
                "TranscriptCorrector: rejeitada por divergência "
                "(%d palavras alteradas > %.1f permitidas): %r",
                distance, allowed_words, corrected,
            )
            return CorrectionResult(
                original, original, CorrectionOutcome.REJECTED_DIVERGENCE,
                edits_rejected=applied + rejected,
                divergence=divergence, latency_ms=latency_ms,
            )

        _logger.debug(
            "TranscriptCorrector: %d edição(ões) aplicada(s) (WER %.2f, %.0f ms): %r -> %r",
            len(applied), divergence, latency_ms, original, corrected,
        )
        return CorrectionResult(
            corrected, original, CorrectionOutcome.APPLIED,
            edits_applied=applied, edits_rejected=rejected,
            divergence=divergence, latency_ms=latency_ms,
        )

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "TranscriptCorrector | None":
        """Build from env; returns None when disabled.

        STT_POSTCORRECT_ENABLED         — 0|1 (default: 0)
        STT_POSTCORRECT_PROVIDER        — openai|gemini|ollama (default: ollama)
        STT_POSTCORRECT_MODEL           — override do modelo do provider
        STT_POSTCORRECT_CONTEXT         — descrição do domínio para o prompt
        STT_POSTCORRECT_TERMS           — termos prováveis (default: WHISPER_HOTWORDS)
        STT_POSTCORRECT_MAX_DIVERGENCE  — WER máximo aceito (default: 0.35)
        STT_POSTCORRECT_SKIP_ABOVE_CONF — pula correção acima desta confiança
                                          (default: 0.85; vazio desativa o skip)
        """
        if os.getenv("STT_POSTCORRECT_ENABLED", "0").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return None
        provider = os.getenv("STT_POSTCORRECT_PROVIDER", "ollama")
        backend = build_backend_from_env(
            provider,
            model_override=os.getenv("STT_POSTCORRECT_MODEL") or None,
            # Correção é tarefa de extração: decode determinístico evita que
            # cada chamada proponha edições diferentes/aventureiras.
            ollama_options={"temperature": 0.0},
        )
        skip_raw = os.getenv("STT_POSTCORRECT_SKIP_ABOVE_CONF", "0.85").strip()
        return cls(
            backend=backend,
            context=os.getenv("STT_POSTCORRECT_CONTEXT", "uma consulta médica"),
            domain_terms=os.getenv(
                "STT_POSTCORRECT_TERMS", os.getenv("WHISPER_HOTWORDS", "")
            ),
            max_divergence=float(os.getenv("STT_POSTCORRECT_MAX_DIVERGENCE", "0.35")),
            skip_above_confidence=float(skip_raw) if skip_raw else None,
        )
