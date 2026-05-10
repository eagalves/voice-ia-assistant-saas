from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...errors import ApiError
from ..context import SessionContext
from ..deps import get_db_session, get_tenant_id
from ..kill_switch import get_kill_switch
from ..metrics import draft_correction_ratio_histogram, draft_kill_switch_gauge
from ..repositories.drafts_repo import DraftsRepository
from ..repositories.feedback_repo import FeedbackRepository
from ..repositories.sessions_repo import SessionsRepository
from ..schemas.drafts import (
    DraftFailedOut,
    DraftFeedbackIn,
    DraftFeedbackOut,
    DraftPendingOut,
    DraftReadyOut,
    DraftRetryOut,
)
from ..tracing import get_tracer

router = APIRouter(prefix="/v1/sessions", tags=["drafts"])


def _attach_context(request: Request, session) -> None:
    request.state.session_context = SessionContext(
        tenant_id=str(session.tenant_id),
        session_id=str(session.id),
        session_kind=session.session_kind,
        seniority=session.seniority,
        actor_role=session.actor_role,
        plan=session.plan,
    )


@router.get(
    "/{session_id}/draft",
    responses={
        200: {"model": DraftReadyOut},
        202: {"model": DraftPendingOut},
        404: {"description": "Session not found"},
        500: {"model": DraftFailedOut},
    },
)
async def get_draft(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
):
    sessions = SessionsRepository(db, tenant_id)
    session = await sessions.get(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )
    _attach_context(request, session)

    drafts = DraftsRepository(db, tenant_id)
    draft = await drafts.get_by_session(session_id)
    if draft is None:
        # /close has not been called yet — treat as pending.
        body = DraftPendingOut(session_id=session_id).model_dump(mode="json")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=body,
            headers={"Retry-After": "5"},
        )

    if draft.draft_status == "pending":
        body = DraftPendingOut(session_id=session_id).model_dump(mode="json")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=body,
            headers={"Retry-After": "5"},
        )
    if draft.draft_status == "ready":
        body = DraftReadyOut(
            session_id=session_id,
            content=draft.content or {},
            updated_at=draft.updated_at,
        ).model_dump(mode="json")
        return JSONResponse(status_code=200, content=body)
    # failed
    body = DraftFailedOut(
        session_id=session_id,
        partial=True,
        content=draft.content,
        last_error=draft.last_error,
    ).model_dump(mode="json")
    return JSONResponse(status_code=500, content=body)


@router.post(
    "/{session_id}/draft/retry",
    response_model=DraftRetryOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_draft(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
) -> DraftRetryOut:
    sessions = SessionsRepository(db, tenant_id)
    session = await sessions.get(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )
    _attach_context(request, session)

    drafts = DraftsRepository(db, tenant_id)
    draft = await drafts.get_by_session(session_id)
    if draft is None:
        raise ApiError(
            code="DRAFT_NOT_FOUND",
            message="Draft has not been created — close the session first.",
            status_code=404,
        )
    if draft.draft_status != "failed":
        raise ApiError(
            code="DRAFT_RETRY_INVALID_STATE",
            message=f"Cannot retry a draft in status '{draft.draft_status}'.",
            status_code=409,
            extra={"draft_status": draft.draft_status},
        )
    await drafts.reset_to_pending(draft)
    return DraftRetryOut(session_id=session_id, retry_count=draft.retry_count)


@router.post(
    "/{session_id}/draft/feedback",
    response_model=DraftFeedbackOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_feedback(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    payload: DraftFeedbackIn,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
) -> DraftFeedbackOut:
    sessions = SessionsRepository(db, tenant_id)
    session = await sessions.get(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )
    _attach_context(request, session)

    if payload.chars_original == 0:
        ratio = 0.0
    else:
        ratio = payload.chars_edited / payload.chars_original

    with get_tracer().start_as_current_span("submit_feedback") as span:
        span.set_attribute("session_id", str(session_id))
        span.set_attribute("tenant_id", tenant_id)

        feedback_repo = FeedbackRepository(db, tenant_id)
        await feedback_repo.insert(
            session_id=session_id,
            chars_original=payload.chars_original,
            chars_edited=payload.chars_edited,
            correction_ratio=ratio,
            fields_corrected=payload.fields_corrected,
            session_kind=session.session_kind,
            actor_sub=payload.actor_sub,
        )

        draft_correction_ratio_histogram.observe(ratio)
        ks = get_kill_switch()
        ks.record(payload.actor_sub or "", ratio)
        if ks.is_active:
            draft_kill_switch_gauge.set(1.0)

    return DraftFeedbackOut(session_id=session_id, correction_ratio=round(ratio, 4))
