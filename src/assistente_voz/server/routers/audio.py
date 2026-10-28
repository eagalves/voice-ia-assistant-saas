"""WebSocket audio ingestion endpoint — Issues #3, #6, #13, #14.

Endpoint:  ``wss://host/v1/sessions/{session_id}/audio``

Query parameters:
- ``ws_token``  — session token (validated against Redis; Issue #6). The
  token record is **bound to the Sessão**: a token issued for another
  ``session_id`` (or tenant) is rejected with ``4001`` even if valid.
- ``speaker``   — ``"doctor"`` (default) or ``"patient"``
- ``tenant_id`` — optional; browsers cannot set custom WebSocket headers, so
  the tenant may come from the query string, the ``X-Tenant-Id`` header
  (server-to-server) or, when absent, the configured default tenant
  (Fase 2.0 single-tenant).

Frame format (JSON text message)::

    {"seq": <int>, "data": "<base64 PCM16/Opus bytes>", "timestamp_ms": <ms>}

Control messages: ``{"type": "reconnect"}`` → ``session_resumed`` handshake;
``{"type": "ping"}`` → ``{"type": "pong"}``.

Server → client messages (``type``): ``status`` (``STT_OVERFLOW``,
``stt_mode`` transitions, ``STT_SEGMENT_FAILED``), ``transcript``,
``red_flag``, ``token_expiring_in``, ``drain``, ``session_resumed``,
``error`` (``FRAME_INVALID`` — the frame is dropped, the socket stays open).

Sticky routing:
  The accept response carries ``X-Session-Id`` so an ALB can forward both
  speaker streams (doctor + patient) to the same container. Set
  ``ECS_CONTAINER_STOP_TIMEOUT=120`` on the task definition.

Close codes:
- 4001 — invalid/expired ws_token, token not bound to this Sessão/tenant,
         unknown Sessão, or infrastructure error during the handshake
- 4002 — Sessão is not open (closed) or was revoked while connected
- 4003 — server is draining (client should reconnect elsewhere)
- 1008 — policy violation (unknown ``speaker``)

All socket writes go through a per-connection outbox owned by one sender
task, so STT callbacks, revocation and expiry warnings never write to the
socket concurrently.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from assistente_voz.medical_rules import detect_red_flags

from ..audio_gateway import AudioGateway
from ..config import get_settings
from ..db.session import get_sessionmaker
from ..drain import DRAIN_RECONNECT_IN, get_drain_manager
from ..event_stream import EventStreamClient
from ..metrics import sessions_active_gauge
from ..redis_keys import ws_revocations_channel, ws_token_key
from ..repositories.sessions_repo import SessionsRepository
from ..stream_client import get_stream_client
from ..stt_service import build_stt_service
from ..transcriber import SessionTranscriber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["audio"])

CLOSE_AUTH_INVALID = 4001
CLOSE_SESSION_REVOKED = 4002
CLOSE_DRAINING = 4003
CLOSE_POLICY_VIOLATION = 1008

ALLOWED_SPEAKERS: tuple[str, ...] = ("doctor", "patient")

# Warn the client 5 minutes before token expiry.
_TOKEN_EXPIRY_WARN_S = 300

_active_connections: int = 0


# ── Token record helpers ─────────────────────────────────────────────────────

def parse_token_record(raw: str | None) -> dict[str, Any] | None:
    """Decode the JSON stored under ``ws:token:{tenant}:{token}``."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def token_matches(record: dict[str, Any], *, session_id: str, tenant_id: str) -> bool:
    """A ws_token is only valid for the Sessão/tenant it was issued for."""
    return (
        str(record.get("session_id", "")) == session_id
        and str(record.get("tenant_id", "")) == tenant_id
    )


# ── Outbox: single writer for the socket ─────────────────────────────────────

class _Outbox:
    """Queue of outbound JSON messages drained by exactly one sender task."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def put_nowait(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    async def put(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    # ``send_json`` lets the outbox stand in for the WebSocket in helpers
    # that only need to emit messages (expiry warning).
    async def send_json(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    async def run(self, websocket: WebSocket) -> None:
        while True:
            message = await self._queue.get()
            try:
                await websocket.send_json(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — socket gone; stop sending
                logger.debug("WS outbox: send failed (%s); stopping sender", type(exc).__name__)
                return


# ── Background helpers ───────────────────────────────────────────────────────

async def _watch_revocation(
    stream: EventStreamClient,
    channel: str,
    ws_token: str,
    websocket: WebSocket,
) -> None:
    """Background task: close WS with 4002 if a revocation is published.

    Uses the stream client's ``subscribe`` (Redis pub/sub in production,
    polling of the in-memory log with ``FakeEventStream``).
    """
    revoke_msg = f"revoke:{ws_token}"
    try:
        async for message in stream.subscribe(channel):
            if message == revoke_msg:
                try:
                    await websocket.close(code=CLOSE_SESSION_REVOKED)
                except Exception:  # noqa: BLE001 — already closed
                    pass
                return
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 — pub/sub failure must not kill the socket
        logger.warning("WS revocation watcher stopped: %s", type(exc).__name__)


async def _token_expiry_warning(
    expires_at_s: float | None,
    websocket: Any,
) -> None:
    """Background task: send a token_expiring_in warning 5 min before expiry."""
    if expires_at_s is None:
        return
    try:
        warn_in = float(expires_at_s) - time.time() - _TOKEN_EXPIRY_WARN_S
    except (TypeError, ValueError):
        return
    if warn_in <= 0:
        return
    try:
        await asyncio.sleep(warn_in)
        await websocket.send_json(
            {"type": "token_expiring_in", "seconds": _TOKEN_EXPIRY_WARN_S}
        )
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        pass


def _resolve_tenant(websocket: WebSocket, query_tenant: str | None) -> str | None:
    """Query param > ``X-Tenant-Id`` header > configured default tenant."""
    raw = (
        (query_tenant or "").strip()
        or (websocket.headers.get("x-tenant-id") or "").strip()
        or get_settings().default_tenant_id
    )
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.websocket("/{session_id}/audio")
async def audio_stream(
    websocket: WebSocket,
    session_id: uuid.UUID,
    ws_token: str = Query(...),
    speaker: str = Query(default="doctor"),
    tenant_id: str | None = Query(default=None),
) -> None:
    """WebSocket endpoint for real-time audio ingestion.

    One connection per speaker per Sessão.  Reconnection is supported:
    if the client sends ``{"type":"reconnect"}`` as the first message
    the gateway responds with ``{"type":"session_resumed","last_seq":N}``.

    Overflow (>50 queued frames) causes a ``{"type":"status","reason":"STT_OVERFLOW"}``
    message to be sent; the WebSocket remains open.

    Close codes: 4001 (token invalid / not bound to this Sessão / lookup
    error), 4002 (Sessão closed or revoked), 4003 (draining), 1008 (bad
    ``speaker``). Any ``except Exception`` around the handshake lookups
    closes with 4001 and never leaks a traceback to the client.
    """
    global _active_connections

    # ── Drain check ───────────────────────────────────────────────────────
    dm = get_drain_manager()
    if dm.draining:
        await websocket.close(code=CLOSE_DRAINING)
        return

    if speaker not in ALLOWED_SPEAKERS:
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    tenant_id_str = _resolve_tenant(websocket, tenant_id)
    if tenant_id_str is None:
        await websocket.close(code=CLOSE_AUTH_INVALID)
        return
    tenant_uuid = uuid.UUID(tenant_id_str)
    session_id_str = str(session_id)

    # ── Validate ws_token via Redis (Issue #6) — bound to this Sessão ─────
    stream = get_stream_client()
    try:
        redis_val = await stream.get(ws_token_key(tenant_id_str, ws_token))
    except Exception:  # noqa: BLE001
        logger.error("Redis error validating ws_token for session %s", session_id)
        await websocket.close(code=CLOSE_AUTH_INVALID)
        return

    record = parse_token_record(redis_val)
    if record is None or not token_matches(
        record, session_id=session_id_str, tenant_id=tenant_id_str
    ):
        await websocket.close(code=CLOSE_AUTH_INVALID)
        return
    expires_at_s = record.get("expires_at")

    # ── Session lookup (short-lived DB session; never pinned to the WS) ───
    try:
        async with get_sessionmaker()() as db:
            session = await SessionsRepository(db, tenant_uuid).get(session_id)
    except Exception:  # noqa: BLE001
        logger.error("DB error fetching session %s for WS", session_id)
        await websocket.close(code=CLOSE_AUTH_INVALID)
        return

    if session is None:
        await websocket.close(code=CLOSE_AUTH_INVALID)
        return
    if session.status != "open":
        await websocket.close(code=CLOSE_SESSION_REVOKED)
        return

    is_medical = session.session_kind == "medical"
    plan_features: dict[str, Any] = dict(session.plan_features or {})
    settings = get_settings()

    # ── Accept connection with sticky-routing header ──────────────────────
    await websocket.accept(headers=[(b"x-session-id", session_id_str.encode("ascii"))])

    _active_connections += 1
    sessions_active_gauge.set(_active_connections, tenant_id=tenant_id_str)

    gateway = AudioGateway(
        session_id=session_id_str,
        tenant_id=tenant_id_str,
        speaker=speaker,
        stream_client=stream,
        is_medical=is_medical,
    )

    outbox = _Outbox()
    sender_task = asyncio.create_task(outbox.run(websocket))

    conn_id = str(uuid.uuid4())

    async def _send_drain() -> None:
        outbox.put_nowait({"type": "drain", "reconnect_in": DRAIN_RECONNECT_IN})

    dm.register(conn_id, _send_drain)

    # ── Background tasks ──────────────────────────────────────────────────
    revocation_task = asyncio.create_task(
        _watch_revocation(
            stream,
            ws_revocations_channel(tenant_id_str),
            ws_token,
            websocket,
        )
    )
    expiry_task = asyncio.create_task(_token_expiry_warning(expires_at_s, outbox))

    # ── STT pipeline (Issue #14) ──────────────────────────────────────────
    transcriber: SessionTranscriber | None = None
    probe_task: asyncio.Task | None = None
    try:
        stt = build_stt_service(
            settings.stt_server_backend,
            is_medical=is_medical,
            on_ws_status=outbox.put_nowait,
            tenant_id=tenant_id_str,
        )
    except ValueError as exc:
        logger.error("STT backend misconfigured: %s", exc)
        stt = None
        outbox.put_nowait({"type": "status", "reason": "STT_BACKEND_MISCONFIGURED"})

    if stt is not None:
        transcriber = SessionTranscriber(
            gateway=gateway,
            stt=stt,
            speaker=speaker,
            send=outbox.put,
            window_s=settings.stt_window_s,
            silence_rms=settings.stt_silence_rms,
            silence_ms=settings.stt_silence_ms,
            realtime_red_flags=is_medical and bool(plan_features.get("llm_realtime_red_flag")),
            red_flag_detector=detect_red_flags,
        )
        gateway.start_consumer(transcriber.handle_frame)
        probe_task = asyncio.create_task(stt.run_probe_loop())
        outbox.put_nowait(
            {
                "type": "status",
                "stt_mode": "streaming",
                "reason": "connected",
                "backend": settings.stt_server_backend,
            }
        )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                outbox.put_nowait(
                    {"type": "error", "reason": "FRAME_INVALID", "detail": "invalid JSON"}
                )
                continue
            if not isinstance(msg, dict):
                outbox.put_nowait(
                    {"type": "error", "reason": "FRAME_INVALID", "detail": "expected object"}
                )
                continue

            # ── Control messages ──────────────────────────────────────
            msg_type = msg.get("type")
            if msg_type == "reconnect":
                outbox.put_nowait(await gateway.reconnect_handshake())
                continue
            if msg_type == "ping":
                outbox.put_nowait({"type": "pong"})
                continue

            # ── Audio frame ───────────────────────────────────────────
            try:
                seq = int(msg["seq"])
                opus_bytes = base64.b64decode(msg["data"], validate=True)
                raw_ts = msg.get("timestamp_ms")
                timestamp_ms = int(raw_ts) if raw_ts is not None else None
            except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                outbox.put_nowait(
                    {
                        "type": "error",
                        "reason": "FRAME_INVALID",
                        "detail": f"{type(exc).__name__}: seq/data/timestamp_ms",
                    }
                )
                continue

            result = await gateway.process_frame(
                seq=seq,
                frame_data=opus_bytes,
                timestamp_ms=timestamp_ms,
            )

            if result.overflow:
                outbox.put_nowait({"type": "status", "reason": "STT_OVERFLOW"})

    except WebSocketDisconnect:
        logger.debug(
            "WS disconnected session=%s speaker=%s", session_id, speaker
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "WS error session=%s speaker=%s: %s", session_id, speaker, exc
        )
    finally:
        revocation_task.cancel()
        expiry_task.cancel()
        if probe_task is not None:
            probe_task.cancel()
        await gateway.stop_consumer()
        if transcriber is not None:
            try:
                # Flush the trailing segment so the last utterance reaches the
                # stream (and the draft) even on abrupt disconnect.
                await transcriber.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("WS transcriber shutdown failed session=%s", session_id)
        sender_task.cancel()
        dm.unregister(conn_id)
        _active_connections = max(0, _active_connections - 1)
        sessions_active_gauge.set(_active_connections, tenant_id=tenant_id_str)
