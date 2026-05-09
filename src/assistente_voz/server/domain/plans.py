"""Plan → feature toggles mapping.

Hard-coded in Python on Issue #1 to keep the surface narrow. When profiles
become dynamic per-tenant (Fase 3), this should be promoted into the
profile YAML loader.
"""

from __future__ import annotations

from typing import Literal, TypedDict

PlanName = Literal["basico", "medio", "premium"]


class PlanFeatures(TypedDict):
    stt_mode: Literal["batch", "streaming"]
    llm_realtime_red_flag: bool
    llm_proactive_questions: bool
    llm_prontuario_model: str
    session_max_minutes: int


PLAN_FEATURES: dict[PlanName, PlanFeatures] = {
    "basico": {
        "stt_mode": "batch",
        "llm_realtime_red_flag": False,
        "llm_proactive_questions": False,
        "llm_prontuario_model": "gpt-4o-mini",
        "session_max_minutes": 60,
    },
    "medio": {
        "stt_mode": "streaming",
        "llm_realtime_red_flag": True,
        "llm_proactive_questions": True,
        "llm_prontuario_model": "gpt-4o-mini",
        "session_max_minutes": 60,
    },
    "premium": {
        "stt_mode": "streaming",
        "llm_realtime_red_flag": True,
        "llm_proactive_questions": True,
        "llm_prontuario_model": "gpt-4o",
        "session_max_minutes": 240,
    },
}


def features_for(plan: PlanName) -> PlanFeatures:
    return PLAN_FEATURES[plan]
