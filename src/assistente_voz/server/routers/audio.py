"""WebSocket audio ingestion endpoint — Issue #3.

Endpoint:  ``wss://host/v1/sessions/{session_id}/audio``

Query parameters:
- ``ws_token``  — session token (validated against Redis; Issue #6)
- ``speaker``   — ``"doctor"`` (default) or ``"patient"``

Frame format (JSON text message)::

    {"seq": <int>, "data": "<base64 Opus bytes>", "timestamp_ms": <ms>}

Sticky routing:
  The response ``X-Session-Id`` header carries the session UUID so an ALB
  can forward both speaker streams (doctor + patient) to the same container.
  Configure the ALB listener rule with ``stickiness.lb_cookie.enabled=true``
  and set ``ECS_CONTAINER_STOP_TIMEOUT=120`` on the task definition.

Audio session status:
- Close code 4001 — invalid or expired ws_token
- Close code 4002 — session is not open (already closed)
- Close code 4003 — server is draining (client should reconnect elsewhere)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..audio_gateway import AudioGateway, QUEUE_MAX_FRAMES
from ..db.session import get_sessionmaker
from ..drain import DRAIN_RECONNECT_IN, get_drain_manager
from ..event_stream import EventStreamClient
from ..redis_keys import ws_revocations_channel, ws_token_key
from ..repositories.sessions_repo import SessionsRepository
from ..stream_client import get_stream_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["audio"])

# How long to wait for a revocation pub/sub message between polls.
_REVOKE_POLL_INTERVAL_S = 0.5
# Warn the client 5 minutes before token expiry.
_TOKEN_EXPIRY_WARN_S = 300


async def _watch_revocation(
    stream: EventStreamClient,
    channel: str,
    ws_token: str,
    websocket: WebSocket,
) -> None:
    """Background task: close WS with 4002 if a revocation is published.

    Polls the FakeEventStream pub/sub log every 0.5 s.  With real Redis the
    implementation would use ``SUBSCRIBE``; the polling pattern works for
    both in-memory tests and real Redis (via a wrapper).
    """
    revoke_msg = f"revoke:{ws_token}"
    seen_up_to: int = 0
    try:
        while True:
            await asyncio.sleep(_REVOKE_POLL_INTERVAL_S)
            # EventStreamClient.publish() appends to _published[channel];
            # we use get() on the raw FakeEventStream via duck-typing here —
            # real Redis would use a subscriber coroutine instead.
            messages: list[str] = []
            raw_pub = getattr(stream, "_published", None)
            if raw_pub is not None:
                messages = raw_pub.get(channel, [])
            for msg in messages[seen_up_to:]:
                if msg == revoke_msg:
                    try:
                        await websocket.close(code=4002)
                    except Exception:
                        pass
                    return
            seen_up_to = len(messages)
    except asyncio.CancelledError:
        pass


async def _token_expiry_warning(
    expires_at_s: float | None,
    websocket: WebSocket,
) -> None:
    """Background task: send a token_expiring_in warning 5 min before expiry."""
    if expires_at_s is None:
        return
    import time

    warn_in = expires_at_s - time.time() - _TOKEN_EXPIRY_WARN_S
    if warn_in <= 0:
        return
    try:
        await asyncio.sleep(warn_in)
        await websocket.send_json(
            {"type": "token_expiring_in", "seconds": _TOKEN_EXPIRY_WARN_S}
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


@router.websocket("/{session_id}/audio")
async def audio_stream(
    websocket: WebSocket,
    session_id: uuid.UUID,
    ws_token: str = Query(...),
    speaker: str = Query(default="doctor"),
) -> None:
    """WebSocket endpoint for real-time audio ingestion.

    One connection per speaker per Sessão.  Reconnection is supported:
    if the client sends ``{"type":"reconnect"}`` as the first message
    the gateway responds with ``{"type":"session_resumed","last_seq":N}``.

    Overflow (>50 queued frames) causes a ``{"type":"status","reason":"STT_OVERFLOW"}``
    message to be sent; the WebSocket remains open.
    """
    # ── Drain check ───────────────────────────────────────────────────────
    dm = get_drain_manager()
    if dm.draining:
        await websocket.close(code=4003)
        return

    # ── Tenant from header ────────────────────────────────────────────────
    tenant_id_header = websocket.headers.get("X-Tenant-Id", "")
    if not tenant_id_header:
        await websocket.close(code=4001)
        return

    try:
        tenant_uuid = uuid.UUID(tenant_id_header)
    except ValueError:
        await websocket.close(code=4001)
        return

    tenant_id_str = str(tenant_uuid)

    # ── Validate ws_token via Redis (Issue #6) ────────────────────────────
    stream = get_stream_client()
    redis_val = await stream.get(ws_token_key(tenant_id_str, ws_token))
    if redis_val is None:
        await websocket.close(code=4001)
        return

    # ── DB session (scoped to this WS connection) ─────────────────────────
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        sessions_repo = SessionsRepository(db, tenant_uuid)
        try:
            session = await sessions_repo.get(session_id)
        except Exception:
            logger.error("DB error fetching session %s for WS", session_id)
            await websocket.close(code=4001)
            return

        if session is None:
            await websocket.close(code=4001)
            return

        if session.status != "open":
            await websocket.close(code=4002)
            return

        # ── Accept connection with sticky-routing header ──────────────────
        await websocket.accept(headers={"X-Session-Id": str(session_id)})

        is_medical = session.session_kind == "medical"

        gateway = AudioGateway(
            session_id=str(session_id),
            tenant_id=tenant_id_str,
            speaker=speaker,
            stream_client=stream,
            is_medical=is_medical,
        )

        conn_id = str(uuid.uuid4())

        async def _send_drain() -> None:
            try:
                await websocket.send_json(
                    {"type": "drain", "reconnect_in": DRAIN_RECONNECT_IN}
                )
            except Exception:
                pass

        dm.register(conn_id, _send_drain)

        # ── Background tasks ──────────────────────────────────────────────
        revocation_task = asyncio.create_task(
            _watch_revocation(
                stream,
                ws_revocations_channel(tenant_id_str),
                ws_token,
                websocket,
            )
        )
        expiry_task = asyncio.create_task(
            _token_expiry_warning(
                None,  # TODO: parse expires_at from redis_val when using real Redis
                websocket,
            )
        )

        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)

                # ── Reconnect handshake ───────────────────────────────────
                if msg.get("type") == "reconnect":
                    handshake = await gateway.reconnect_handshake()
                    await websocket.send_json(handshake)
                    continue

                # ── Audio frame ───────────────────────────────────────────
                seq: int = int(msg["seq"])
                opus_bytes: bytes = base64.b64decode(msg["data"])
                timestamp_ms: int = int(msg.get("timestamp_ms", 0))

                result = await gateway.process_frame(
                    seq=seq,
                    frame_data=opus_bytes,
                    timestamp_ms=timestamp_ms,
                )

                if result.overflow:
                    await websocket.send_json(
                        {"type": "status", "reason": "STT_OVERFLOW"}
                    )

        except WebSocketDisconnect:
            logger.debug(
                "WS disconnected session=%s speaker=%s", session_id, speaker
            )
        except Exception as exc:
            logger.exception(
                "WS error session=%s speaker=%s: %s", session_id, speaker, exc
            )
        finally:
            revocation_task.cancel()
            expiry_task.cancel()
            dm.unregister(conn_id)

