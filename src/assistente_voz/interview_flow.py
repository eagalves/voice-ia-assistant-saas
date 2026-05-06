from __future__ import annotations

from dataclasses import dataclass, field


ALLOWED_SENIORITY = {"junior", "pleno", "senior"}


@dataclass(frozen=True)
class InterviewQuestion:
    group: str
    text: str


@dataclass(frozen=True)
class InterviewResult:
    recommendation: str
    sections: dict[str, str]


class InterviewFlow:
    """Interview flow with seniority-aware tracks and one-question-at-a-time policy."""

    def __init__(self, profile_data: dict, seniority_level: str) -> None:
        level = seniority_level.strip().lower()
        if level not in ALLOWED_SENIORITY:
            raise ValueError("Invalid seniority level")

        self._profile = profile_data
        self._level = level
        self._question_bank = profile_data.get("question_bank", {})
        self._rules = (profile_data.get("seniority_rules", {}) or {}).get(level, {})
        self._queue: list[InterviewQuestion] = self._build_queue()
        self._asked: list[InterviewQuestion] = []

    def _build_queue(self) -> list[InterviewQuestion]:
        groups = self._rules.get("mandatory_question_groups") or list(self._question_bank.keys())
        queue: list[InterviewQuestion] = []
        for group in groups:
            for q in self._question_bank.get(group, []):
                queue.append(InterviewQuestion(group=group, text=q))
        return queue

    def next_question(self) -> InterviewQuestion | None:
        """Return exactly one question per call."""
        if not self._queue:
            return None
        q = self._queue.pop(0)
        self._asked.append(q)
        return q

    def evaluate(self, competency_scores: dict[str, float]) -> InterviewResult:
        """Generate recommendation based on weighted competency scores."""
        weights = self._rules.get("weighting", {})
        weighted_score = 0.0
        total_weight = 0.0
        for comp, weight in weights.items():
            score = competency_scores.get(comp, 0.0)
            weighted_score += score * weight
            total_weight += weight

        normalized = weighted_score / total_weight if total_weight else 0.0
        recommendation = self._recommendation_for_score(normalized)

        sections = {
            "resumo_do_candidato": "Resumo objetivo da entrevista.",
            "pontos_fortes": "Competências com melhor evidência.",
            "pontos_de_risco": "Lacunas observadas durante a entrevista.",
            "aderencia_ao_nivel": f"Trilha avaliada para senioridade: {self._level}.",
            "recomendacao_final": recommendation,
            "perguntas_de_follow_up": "Perguntas complementares sugeridas.",
        }
        return InterviewResult(recommendation=recommendation, sections=sections)

    def minimum_questions(self) -> int:
        return int(self._rules.get("minimum_questions", 0))

    def _recommendation_for_score(self, score: float) -> str:
        if score >= 4.0:
            return "aprovado"
        if score >= 3.0:
            return "aprovado_com_ressalvas"
        if score >= 2.0:
            return "banco_de_talentos"
        return "nao_aprovado"
