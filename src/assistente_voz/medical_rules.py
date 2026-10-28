from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


REQUIRED_FIELDS = [
    "queixa_principal",
    "inicio",
    "duracao",
    "intensidade",
    "sintomas_associados",
    "medicacoes_em_uso",
    "alergias",
    "sinais_de_alerta_atuais",
]

# Canonical red-flag terms, written accent-free. Matching is done on
# accent-stripped, lower-cased text (see ``normalize_clinical_text``), so a
# transcription such as "convulsão" or "ideação suicida" matches the entries
# below. Keep terms as whole-word phrases; the PRD battery examples
# ("dor torácica irradiando para o braço", "perda de consciência súbita")
# are covered by "dor toracica" and "perda de consciencia".
RED_FLAG_TERMS = {
    "dor no peito",
    "dor toracica",
    "dispneia",
    "falta de ar",
    "desmaio",
    "perda de consciencia",
    "sincope",
    "convulsao",
    "sangramento intenso",
    "ideacao suicida",
}


def normalize_clinical_text(text: str) -> str:
    """Lower-case, strip accents and collapse whitespace/punctuation.

    Whisper writes correct pt-BR orthography ("convulsão"); the canonical
    term list is accent-free so both sides must be folded before matching.
    """
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return f" {folded.strip()} "


def detect_red_flags(text: str) -> list[str]:
    """Return the sorted canonical red-flag terms present in *text* (whole words)."""
    if not text:
        return []
    normalized = normalize_clinical_text(text)
    return sorted(term for term in RED_FLAG_TERMS if f" {term} " in normalized)


@dataclass(frozen=True)
class MedicalRecord:
    status: str  # partial | final
    missing_fields: list[str]
    red_flags_detected: list[str]
    sections: dict[str, str]


class MedicalProfileEngine:
    """Apply medical profile rules to build partial/final records safely."""

    def __init__(self, required_fields: list[str] | None = None) -> None:
        self._required_fields = required_fields or REQUIRED_FIELDS

    def build_record(self, collected: dict[str, str]) -> MedicalRecord:
        missing = [f for f in self._required_fields if not str(collected.get(f, "")).strip()]

        narrative = " ".join(str(v) for v in collected.values() if v)
        red_flags = self._detect_red_flags(narrative)

        status = "partial" if missing else "final"
        sections: dict[str, str] = {
            "resumo_do_caso": self._build_case_summary(collected),
            "perguntas_faltantes": ", ".join(missing) if missing else "nenhuma",
            "sinais_de_alerta": ", ".join(red_flags) if red_flags else "nenhum identificado",
            "hipoteses_possiveis": "Hipóteses iniciais para discussão clínica.",
            "exames_sugeridos_para_discutir": "Sugerir exames conforme avaliação profissional.",
            "orientacao_final_de_seguranca": self._safety_guidance(bool(red_flags)),
        }

        return MedicalRecord(
            status=status,
            missing_fields=missing,
            red_flags_detected=red_flags,
            sections=sections,
        )

    def _build_case_summary(self, collected: dict[str, str]) -> str:
        qp = collected.get("queixa_principal", "não informado")
        inicio = collected.get("inicio", "não informado")
        duracao = collected.get("duracao", "não informado")
        return f"Queixa principal: {qp}. Início: {inicio}. Duração: {duracao}."

    def _detect_red_flags(self, text: str) -> list[str]:
        return detect_red_flags(text)

    def _safety_guidance(self, has_red_flags: bool) -> str:
        if has_red_flags:
            return (
                "Sinais de alerta identificados. Recomendar avaliação presencial imediata "
                "ou serviço de emergência conforme gravidade."
            )
        return "Orientar acompanhamento profissional e retorno se houver piora."
