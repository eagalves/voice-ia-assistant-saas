"""WebSocket audio ingestion endpoint — Issue #3.

Endpoint:  ``wss://host/v1/sessions/{session_id}/audio``

Query parameters:
- ``ws_token``  — session token (validated against ``sessions.ws_token``)
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

import base64
import json
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..audio_gateway import AudioGateway, QUEUE_MAX_FRAMES
from ..db.session import get_sessionmaker
from ..drain import DRAIN_RECONNECT_IN, get_drain_manager
from ..event_stream import FakeEventStream
from ..repositories.sessions_repo import SessionsRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["audio"])


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

    # ── DB session (scoped to this WS connection) ─────────────────────────
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        # ── Validate ws_token against sessions table ───────────────────────
        tenant_id_header = websocket.headers.get("X-Tenant-Id", "")
        if not tenant_id_header:
            await websocket.close(code=4001)
            return

        try:
            tenant_uuid = uuid.UUID(tenant_id_header)
        except ValueError:
            await websocket.close(code=4001)
            return

        sessions_repo = SessionsRepository(db, tenant_uuid)
        session = await sessions_repo.get(session_id)

        if session is None or session.ws_token != ws_token:
            await websocket.close(code=4001)
            return

        if session.status != "open":
            await websocket.close(code=4002)
            return

        # ── Accept connection with sticky-routing header ──────────────────
        await websocket.accept(
            headers={"X-Session-Id": str(session_id)}
        )

        is_medical = session.session_kind == "medical"

        gateway = AudioGateway(
            session_id=str(session_id),
            tenant_id=str(tenant_uuid),
            speaker=speaker,
            stream_client=FakeEventStream(),  # TODO: inject real redis.asyncio client
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
            dm.unregister(conn_id)
