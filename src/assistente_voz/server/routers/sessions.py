from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...errors import ApiError
from ..config import ServerSettings
from ..context import SessionContext
from ..deps import check_not_draining, get_app_settings, get_db_session, get_tenant_id
from ..domain.plans import features_for
from ..materialization import MaterializationWorker
from ..redis_keys import ws_revocations_channel, ws_token_key
from ..repositories.drafts_repo import DraftsRepository
from ..repositories.idempotency_repo import (
    ENDPOINT_CREATE_SESSION,
    IdempotencyRepository,
    canonical_hash,
)
from ..repositories.sessions_repo import SessionsRepository
from ..schemas.sessions import (
    CloseSessionOut,
    CreateSessionIn,
    CreateSessionOut,
    TokenRefreshOut,
)
from ..stream_client import get_stream_client

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


def _idempotency_key_or_400(idempotency_key: str | None) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise ApiError(
            code="IDEMPOTENCY_KEY_MISSING",
            message="Header `Idempotency-Key` is required.",
            status_code=400,
        )
    return idempotency_key.strip()


def _build_session_context(
    *,
    tenant_id: str,
    session_id: uuid.UUID,
    session_kind: str,
    seniority: str | None,
    actor_role: str,
    plan: str,
) -> SessionContext:
    return SessionContext(
        tenant_id=tenant_id,
        session_id=str(session_id),
        session_kind=session_kind,
        seniority=seniority,
        actor_role=actor_role,
        plan=plan,
    )


@router.post(
    "",
    response_model=CreateSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: CreateSessionIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[ServerSettings, Depends(get_app_settings)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _drain: None = Depends(check_not_draining),
) -> Response | CreateSessionOut:
    key = _idempotency_key_or_400(idempotency_key)

    # Use JWT sub claim (set by middleware); falls back to raw bearer in bypass mode.
    actor_sub = getattr(request.state, "actor_sub", request.state.bearer_token)

    body_dict = payload.model_dump()
    p_hash = canonical_hash(body_dict)

    idempotency_repo = IdempotencyRepository(db, tenant_id)

    existing = await idempotency_repo.get_valid(ENDPOINT_CREATE_SESSION, actor_sub, key)
    if existing is not None:
        if existing.payload_hash == p_hash:
            # Replay: return the cached response with replay header.
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
                headers={"Idempotency-Replayed": "true"},
            )
        raise ApiError(
            code="IDEMPOTENCY_PAYLOAD_MISMATCH",
            message=(
                "Idempotency-Key already used with a different request payload. "
                "Use a new Idempotency-Key for a different request."
            ),
            status_code=409,
        )

    plan_features = features_for(payload.plan)

    repo = SessionsRepository(db, tenant_id)
    ws_token = uuid.uuid4()
    session = await repo.create(
        session_kind=payload.session_kind,
        actor_role=payload.actor_role,
        plan=payload.plan,
        seniority=payload.seniority,
        plan_features=dict(plan_features),
        idempotency_key=key,
        ws_token=ws_token,
    )

    request.state.session_context = _build_session_context(
        tenant_id=tenant_id,
        session_id=session.id,
        session_kind=payload.session_kind,
        seniority=payload.seniority,
        actor_role=payload.actor_role,
        plan=payload.plan,
    )

    ws_url = f"{settings.public_ws_base.rstrip('/')}/v1/sessions/{session.id}/audio"
    out = CreateSessionOut(
        session_id=session.id,
        ws_url=ws_url,
        ws_token=ws_token,
        session_kind=payload.session_kind,
        plan_features=plan_features,
        auto_finalize_at=session.auto_finalize_at,
    )

    # Serialise and store idempotency record.
    response_body = out.model_dump(mode="json")
    await idempotency_repo.store(
        endpoint=ENDPOINT_CREATE_SESSION,
        actor_sub_jwt=actor_sub,
        idempotency_key=key,
        payload_hash=p_hash,
        response_body=response_body,
        response_status=status.HTTP_201_CREATED,
    )

    # Store ws_token in Redis with TTL = session_max_minutes.
    token_ttl_s = plan_features["session_max_minutes"] * 60
    stream = get_stream_client()
    token_data = json.dumps(
        {
            "session_id": str(session.id),
            "tenant_id": tenant_id,
            "plan": payload.plan,
        }
    )
    await stream.set(ws_token_key(tenant_id, str(ws_token)), token_data, ex=token_ttl_s)

    return out


@router.post("/{session_id}/close", response_model=CloseSessionOut)
async def close_session(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: Annotated[uuid.UUID, Path()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
) -> CloseSessionOut:
    sessions = SessionsRepository(db, tenant_id)
    drafts = DraftsRepository(db, tenant_id)

    session = await sessions.get(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )
    if session.status == "closed":
        raise ApiError(
            code="SESSION_ALREADY_CLOSED",
            message="Session is already closed.",
            status_code=409,
        )

    await sessions.mark_closed(session)

    # Revoke ws_token in Redis + publish revocation event.
    old_ws_token = session.ws_token
    stream = get_stream_client()
    await stream.delete(ws_token_key(tenant_id, old_ws_token))
    await stream.publish(
        ws_revocations_channel(tenant_id), f"revoke:{old_ws_token}"
    )

    existing = await drafts.get_by_session(session_id)
    if existing is None:
        await drafts.insert_pending(session_id)
    # If a draft already exists (defensive), leave it untouched —
    # The worker is the source of truth for transitions.

    worker = MaterializationWorker(
        tenant_id=tenant_id,
        session_id=str(session_id),
    )
    background_tasks.add_task(worker.run)

    request.state.session_context = _build_session_context(
        tenant_id=tenant_id,
        session_id=session.id,
        session_kind=session.session_kind,
        seniority=session.seniority,
        actor_role=session.actor_role,
        plan=session.plan,
    )

    assert session.closed_at is not None
    return CloseSessionOut(
        session_id=session.id,
        closed_at=session.closed_at,
        draft_status="pending",
    )


@router.post("/{session_id}/token/refresh", response_model=TokenRefreshOut)
async def refresh_ws_token(
    request: Request,
    session_id: Annotated[uuid.UUID, Path()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
) -> TokenRefreshOut:
    """Issue #1 stub.

    Returns a fresh `ws_token` and persists it on the row, but does NOT
    invalidate the old token in Redis (Issue #6) nor publish a revocation
    event. The current WS connection (if any) is unaffected.
    """
    sessions = SessionsRepository(db, tenant_id)
    session = await sessions.get(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )

    new_token = uuid.uuid4()
    old_token = session.ws_token
    await sessions.rotate_ws_token(session, new_token)

    # Atomically swap ws_token in Redis: DEL old, SET new.
    stream = get_stream_client()
    await stream.delete(ws_token_key(tenant_id, old_token))
    token_ttl_s = session.plan_features.get("session_max_minutes", 60) * 60
    token_data = json.dumps(
        {"session_id": str(session.id), "tenant_id": tenant_id, "plan": session.plan}
    )
    await stream.set(ws_token_key(tenant_id, str(new_token)), token_data, ex=token_ttl_s)

    request.state.session_context = _build_session_context(
        tenant_id=tenant_id,
        session_id=session.id,
        session_kind=session.session_kind,
        seniority=session.seniority,
        actor_role=session.actor_role,
        plan=session.plan,
    )

    return TokenRefreshOut(
        session_id=session.id,
        ws_token=new_token,
        issued_at=datetime.now(timezone.utc),
    )
