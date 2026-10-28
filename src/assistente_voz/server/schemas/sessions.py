from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ..domain.plans import PlanFeatures, PlanName

SessionKind = Literal[
    "medical",
    "general_interviewer",
    "clinical_pharmacist_interviewer",
    "clinical_nurse_interviewer",
]
ActorRole = Literal["doctor", "patient", "physician", "candidate", "interviewer"]
Seniority = Literal["junior", "pleno", "senior"]


class CreateSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_kind: SessionKind
    actor_role: ActorRole
    plan: PlanName
    seniority: Seniority | None = None

    @field_validator("seniority")
    @classmethod
    def _seniority_required_for_interviewers(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        # Mirrors bootstrap._validate_profile_params: interviewer profiles are
        # parameterised by seniority (profiles/README.md).
        kind = info.data.get("session_kind")
        if kind and "interviewer" in kind and value is None:
            raise ValueError(
                "seniority is required for interviewer session kinds (junior|pleno|senior)"
            )
        return value


class CreateSessionOut(BaseModel):
    session_id: UUID
    ws_url: str
    ws_token: UUID
    ws_token_expires_at: datetime
    session_kind: SessionKind
    plan_features: PlanFeatures = Field(...)
    auto_finalize_at: datetime


class CloseSessionOut(BaseModel):
    session_id: UUID
    closed_at: datetime
    draft_status: Literal["pending"]


class TokenRefreshOut(BaseModel):
    session_id: UUID
    ws_token: UUID
    ws_token_expires_at: datetime
    issued_at: datetime
