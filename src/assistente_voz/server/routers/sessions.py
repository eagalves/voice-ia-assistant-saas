from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Request, Response, status
from fastapi.responses import JSONResponse

from ...errors import ApiError
from ..audit_log import EVENT_SESSION_CREATED, EVENT_SESSION_TOKEN_REFRESHED, log_event
from ..config import ServerSettings
from ..context import SessionContext
from ..db.models import Session
from ..deps import (
    DbSession,
    check_not_draining,
    get_actor_sub,
    get_app_settings,
    get_tenant_id,
)
from ..domain.plans import features_for
from ..redis_keys import ws_token_key
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
from ..session_lifecycle import (
    REASON_CLIENT_CLOSE,
    close_session_tx,
    materialization_worker_for,
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


def _token_record(session: Session, tenant_id: str, expires_at: datetime) -> str:
    """JSON stored in Redis under ``ws:token:{tenant}:{token}``.

    ``session_id``/``tenant_id`` bind the token to one Sessão (checked by the
    WS handshake); ``expires_at`` (epoch seconds) drives the
    ``token_expiring_in`` warning.
    """
    return json.dumps(
        {
            "session_id": str(session.id),
            "tenant_id": tenant_id,
            "plan": session.plan,
            "expires_at": expires_at.timestamp(),
        }
    )


def _hard_cap_at(session: Session) -> datetime:
    """Absolute instant after which no ws_token may be valid for *session*.

    ``min(created_at + session_max_minutes, auto_finalize_at)``: the plan's
    hard cap (60 min Básico/Médio, 240 min Premium) or the 6h auto-finalize,
    whichever comes first. Token refresh can never extend a Sessão past it.
    """
    max_minutes = int((session.plan_features or {}).get("session_max_minutes", 60))
    created_at = session.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    plan_cap = created_at + timedelta(minutes=max_minutes)
    auto_finalize_at = session.auto_finalize_at
    if auto_finalize_at.tzinfo is None:
        auto_finalize_at = auto_finalize_at.replace(tzinfo=timezone.utc)
    return min(plan_cap, auto_finalize_at)


@router.post(
    "",
    response_model=CreateSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: CreateSessionIn,
    request: Request,
    db: DbSession,
    settings: Annotated[ServerSettings, Depends(get_app_settings)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    actor_sub: Annotated[str, Depends(get_actor_sub)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _drain: None = Depends(check_not_draining),
) -> Response | CreateSessionOut:
    key = _idempotency_key_or_400(idempotency_key)

    # JWT sub claim (set by middleware); raw bearer in bypass mode.
    actor_sub = actor_sub or getattr(request.state, "bearer_token", "")

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

    token_ttl_s = plan_features["session_max_minutes"] * 60
    token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_ttl_s)

    ws_url = f"{settings.public_ws_base.rstrip('/')}/v1/sessions/{session.id}/audio"
    out = CreateSessionOut(
        session_id=session.id,
        ws_url=ws_url,
        ws_token=ws_token,
        ws_token_expires_at=token_expires_at,
        session_kind=payload.session_kind,
        plan_features=plan_features,
        auto_finalize_at=session.auto_finalize_at,
    )
    response_body = out.model_dump(mode="json")

    # First-write-wins under concurrency: INSERT … ON CONFLICT DO NOTHING
    # blocks until a concurrent writer with the same scope commits; if the
    # canonical record is not ours we lost the race → discard our Sessão and
    # replay the winner (the client never sees two different session_ids).
    stored = await idempotency_repo.store(
        endpoint=ENDPOINT_CREATE_SESSION,
        actor_sub_jwt=actor_sub,
        idempotency_key=key,
        payload_hash=p_hash,
        response_body=response_body,
        response_status=status.HTTP_201_CREATED,
    )
    if stored is not None and stored.response_body != response_body:
        # Read the winner's row before rollback: rollback expires ORM state and
        # a later attribute access would need IO (MissingGreenlet).
        winner_status = stored.response_status
        winner_body = dict(stored.response_body)
        winner_hash = stored.payload_hash
        await db.rollback()
        if winner_hash != p_hash:
            raise ApiError(
                code="IDEMPOTENCY_PAYLOAD_MISMATCH",
                message=(
                    "Idempotency-Key already used with a different request payload. "
                    "Use a new Idempotency-Key for a different request."
                ),
                status_code=409,
            )
        return JSONResponse(
            status_code=winner_status,
            content=winner_body,
            headers={"Idempotency-Replayed": "true"},
        )

    await log_event(
        db,
        tenant_id=tenant_id,
        event_type=EVENT_SESSION_CREATED,
        session_id=session.id,
        actor_sub=actor_sub,
        payload={
            "session_kind": payload.session_kind,
            "plan": payload.plan,
            "actor_role": payload.actor_role,
            "seniority": payload.seniority,
            "auto_finalize_at": session.auto_finalize_at.isoformat(),
        },
    )

    request.state.session_context = _build_session_context(
        tenant_id=tenant_id,
        session_id=session.id,
        session_kind=payload.session_kind,
        seniority=payload.seniority,
        actor_role=payload.actor_role,
        plan=payload.plan,
    )

    # Store ws_token in Redis with TTL = session_max_minutes (bound to the Sessão).
    stream = get_stream_client()
    await stream.set(
        ws_token_key(tenant_id, str(ws_token)),
        _token_record(session, tenant_id, token_expires_at),
        ex=token_ttl_s,
    )

    return out


@router.post("/{session_id}/close", response_model=CloseSessionOut)
async def close_session(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    actor_sub: Annotated[str, Depends(get_actor_sub)],
) -> CloseSessionOut:
    sessions = SessionsRepository(db, tenant_id)

    session = await sessions.get_for_update(session_id)
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

    stream = get_stream_client()
    await close_session_tx(
        db,
        tenant_id=tenant_id,
        session=session,
        stream=stream,
        reason=REASON_CLIENT_CLOSE,
        actor_sub=actor_sub or None,
    )

    worker = materialization_worker_for(
        session, tenant_id=tenant_id, actor_sub=actor_sub or None, stream=stream
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
    db: DbSession,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    actor_sub: Annotated[str, Depends(get_actor_sub)],
) -> TokenRefreshOut:
    """Issue a fresh ``ws_token`` for an open Sessão (Issue #6).

    Invalidates the previous token atomically in Redis (DEL old + SET new)
    without closing the active WS; the app hands the new token to the
    browser through its own channel. The new TTL never extends the Sessão
    past its hard cap (plan ``session_max_minutes`` / auto-finalize).
    """
    sessions = SessionsRepository(db, tenant_id)
    session = await sessions.get_for_update(session_id)
    if session is None:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} not found.",
            status_code=404,
        )
    if session.status != "open":
        raise ApiError(
            code="SESSION_ALREADY_CLOSED",
            message="Cannot refresh the token of a closed session.",
            status_code=409,
        )

    now = datetime.now(timezone.utc)
    hard_cap_at = _hard_cap_at(session)
    remaining_s = int((hard_cap_at - now).total_seconds())
    if remaining_s <= 0:
        raise ApiError(
            code="SESSION_HARD_CAP_REACHED",
            message="Session reached its hard cap; close it and create a new one.",
            status_code=409,
            extra={"hard_cap_at": hard_cap_at.isoformat()},
        )

    new_token = uuid.uuid4()
    old_token = session.ws_token
    await sessions.rotate_ws_token(session, new_token)

    token_ttl_s = remaining_s
    token_expires_at = now + timedelta(seconds=token_ttl_s)

    # Atomically swap ws_token in Redis: DEL old, SET new.
    stream = get_stream_client()
    await stream.delete(ws_token_key(tenant_id, old_token))
    await stream.set(
        ws_token_key(tenant_id, str(new_token)),
        _token_record(session, tenant_id, token_expires_at),
        ex=token_ttl_s,
    )

    await log_event(
        db,
        tenant_id=tenant_id,
        event_type=EVENT_SESSION_TOKEN_REFRESHED,
        session_id=session.id,
        actor_sub=actor_sub or None,
        payload={"expires_at": token_expires_at.isoformat(), "ttl_s": token_ttl_s},
    )

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
        ws_token_expires_at=token_expires_at,
        issued_at=now,
    )
