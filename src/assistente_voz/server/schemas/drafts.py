from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DraftPendingOut(BaseModel):
    error: Literal["DRAFT_NOT_READY"] = "DRAFT_NOT_READY"
    session_id: UUID
    draft_status: Literal["pending"] = "pending"
    retry_after_seconds: int = 5


class DraftReadyOut(BaseModel):
    session_id: UUID
    draft_status: Literal["ready"] = "ready"
    content: dict[str, Any]
    updated_at: datetime


class DraftFailedOut(BaseModel):
    error: Literal["DRAFT_MATERIALIZATION_FAILED"] = "DRAFT_MATERIALIZATION_FAILED"
    session_id: UUID
    draft_status: Literal["failed"] = "failed"
    partial: bool = True
    content: dict[str, Any] | None = None
    last_error: str | None = None


class DraftRetryOut(BaseModel):
    session_id: UUID
    draft_status: Literal["pending"] = "pending"
    retry_count: int


class DraftFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chars_original: int = Field(ge=0)
    chars_edited: int = Field(ge=0)
    fields_corrected: list[str] = Field(default_factory=list)
    actor_sub: str | None = None


class DraftFeedbackOut(BaseModel):
    session_id: UUID
    correction_ratio: float
    accepted: bool = True
