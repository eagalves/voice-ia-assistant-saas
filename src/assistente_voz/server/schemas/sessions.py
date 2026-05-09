from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class CreateSessionOut(BaseModel):
    session_id: UUID
    ws_url: str
    ws_token: UUID
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
    issued_at: datetime
