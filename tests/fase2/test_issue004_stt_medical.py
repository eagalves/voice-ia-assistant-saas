"""Tests for Issue #4 — STT Medical Completude-First with STT_CONFIDENCE and
Fallback Batch.

Also covers Gap tests from Issue #3:
  GAP-TTL1 — stream key TTL set after AudioGateway.close()
  GAP-SAD1 — abrupt WS disconnect: no unhandled exception, DrainManager unregistered
  GAP-WS1  — full WS overflow flow via starlette TestClient
  GAP-WS2  — reconnect handshake via starlette TestClient

Acceptance criteria:
  AC1  — STT_CONFIDENCE=0.45 → segments with confidence < 0.45 receive uncertain=True
  AC2  — STT_CONFIDENCE absent → warning + effective 0.55
  AC3  — STT_CONFIDENCE=0.20 (below 0.30) → warning + fallback 0.55
  AC4  — Deepgram timeout > 800 ms → frame routed to Whisper batch
  AC5  — 3 consecutive Deepgram failures → CB opens + WS status event
  AC6  — Whisper batch processes audio during CB open (no session lost)
  AC7  — 5 health probes OK after ≥ 60 s → CB closes + WS event
  AC8  — medical + MEDICAL_STT_PARTIAL_ENABLED=0 → no partial events emitted
  AC9  — uncertain=True segment → apply_incerto_prefix adds [incerto] prefix
  AC10 — STTService.transcribe() legacy coexists with STTStreamService

Notes:
- asyncio_mode=auto (pyproject.toml) — do NOT add pytestmark = pytest.mark.asyncio
- STTStreamService and STTCircuitBreaker tested directly (no WS server needed for AC1-AC10)
- GAP-WS1 / GAP-WS2 use starlette.testclient.TestClient (sync) from within async tests
"""
from __future__ import annotations

import base64
import json
import logging
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.audio_gateway import (
    QUEUE_MAX_FRAMES,
    STREAM_TTL_SECONDS,
    AudioGateway,
    PassthroughDecoder,
)
from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.drain import get_drain_manager, reset_drain_manager
from assistente_voz.server.event_stream import FakeEventStream
from assistente_voz.server.redis_keys import session_events_key
from assistente_voz.server.stt_service import (
    CB_FAILURE_THRESHOLD,
    CB_HEALTH_THRESHOLD,
    CB_MIN_OPEN_DURATION_S,
    STT_CONFIDENCE_DEFAULT,
    STT_CONFIDENCE_MIN,
    FakeBatchBackend,
    FakeStreamingBackend,
    STTCircuitBreaker,
    STTStreamService,
    apply_incerto_prefix,
    get_confidence_threshold,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _service(
    *,
    streaming: FakeStreamingBackend | None = None,
    batch: FakeBatchBackend | None = None,
    confidence: float = 0.55,
    is_medical: bool = False,
    medical_partial: bool = False,
    ws_status_events: list | None = None,
    cb: STTCircuitBreaker | None = None,
    timeout_s: float = 0.05,   # fast timeout for tests
) -> STTStreamService:
    captured: list[dict] = ws_status_events if ws_status_events is not None else []
    return STTStreamService(
        streaming_backend=streaming or FakeStreamingBackend(),
        batch_backend=batch or FakeBatchBackend(),
        confidence_threshold=confidence,
        is_medical=is_medical,
        medical_partial_enabled=medical_partial,
        on_ws_status=captured.append,
        _circuit_breaker=cb,
        _timeout_s=timeout_s,
    )


def _fake_cb(*, fake_time: list[float]) -> STTCircuitBreaker:
    """STTCircuitBreaker with injectable clock for time-based tests."""
    return STTCircuitBreaker(clock=lambda: fake_time[0])


# ── AC1: valid STT_CONFIDENCE ─────────────────────────────────────────────────

async def test_ac1_confidence_valid_threshold():
    """STT_CONFIDENCE=0.45 → segments with confidence < 0.45 receive uncertain=True."""
    threshold = get_confidence_threshold("0.45")
    assert threshold == pytest.approx(0.45)

    finals: list = []
    svc = _service(
        streaming=FakeStreamingBackend(result_confidence=0.40),
        confidence=threshold,
    )
    await svc.process_chunk(b"audio", on_final=finals.append)

    assert len(finals) == 1
    assert finals[0].uncertain is True


async def test_ac1_above_threshold_not_uncertain():
    """Segment with confidence above threshold → uncertain=False."""
    finals: list = []
    svc = _service(
        streaming=FakeStreamingBackend(result_confidence=0.80),
        confidence=0.45,
    )
    await svc.process_chunk(b"audio", on_final=finals.append)
    assert finals[0].uncertain is False


# ── AC2: STT_CONFIDENCE absent ────────────────────────────────────────────────

def test_ac2_confidence_absent_warns_and_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """STT_CONFIDENCE not set → warning logged + effective 0.55."""
    with caplog.at_level(logging.WARNING, logger="assistente_voz.server.stt_service"):
        value = get_confidence_threshold(None)
    assert value == pytest.approx(STT_CONFIDENCE_DEFAULT)
    assert any("not set" in r.message for r in caplog.records)


# ── AC3: STT_CONFIDENCE below minimum ────────────────────────────────────────

def test_ac3_confidence_below_min_warns_and_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """STT_CONFIDENCE=0.20 (below 0.30) → warning logged + effective 0.55."""
    with caplog.at_level(logging.WARNING, logger="assistente_voz.server.stt_service"):
        value = get_confidence_threshold("0.20")
    assert value == pytest.approx(STT_CONFIDENCE_DEFAULT)
    assert any("outside" in r.message for r in caplog.records)


def test_ac3_confidence_above_max_warns_and_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """STT_CONFIDENCE=0.99 (above 0.95) → warning logged + effective 0.55."""
    with caplog.at_level(logging.WARNING, logger="assistente_voz.server.stt_service"):
        value = get_confidence_threshold("0.99")
    assert value == pytest.approx(STT_CONFIDENCE_DEFAULT)
    assert any("outside" in r.message for r in caplog.records)


# ── AC4: Deepgram timeout → batch ────────────────────────────────────────────

async def test_ac4_timeout_routes_chunk_to_batch():
    """Deepgram timeout > 800 ms → frame re-queued to Whisper batch."""
    batch = FakeBatchBackend(result_text="batch result")
    finals: list = []
    svc = _service(
        streaming=FakeStreamingBackend(timeout=True),
        batch=batch,
        timeout_s=0.01,   # 10 ms — streaming backend sleeps 3600 s → cancelled
    )
    await svc.process_chunk(b"frame", on_final=finals.append)

    assert batch.call_count == 1
    assert len(finals) == 1
    assert finals[0].text == "batch result"


# ── AC5: circuit breaker opens after 3 failures ───────────────────────────────

async def test_ac5_circuit_breaker_opens_after_3_failures():
    """3 consecutive Deepgram failures → CB opens + WS status event emitted."""
    ws_events: list[dict] = []
    streaming = FakeStreamingBackend(fail=True)
    batch = FakeBatchBackend()
    svc = _service(streaming=streaming, batch=batch, ws_status_events=ws_events)

    for _ in range(CB_FAILURE_THRESHOLD):
        await svc.process_chunk(b"frame", on_final=lambda _: None)

    assert svc.is_degraded
    assert any(
        e.get("stt_mode") == "fallback_batch" for e in ws_events
    ), f"Expected fallback_batch event; got: {ws_events}"


async def test_ac5_cb_opens_exactly_at_threshold():
    """CB does not open before the 3rd failure."""
    svc = _service(streaming=FakeStreamingBackend(fail=True))

    # First two failures — should not open
    for _ in range(CB_FAILURE_THRESHOLD - 1):
        await svc.process_chunk(b"frame", on_final=lambda _: None)

    assert not svc.is_degraded

    # Third failure — opens
    await svc.process_chunk(b"frame", on_final=lambda _: None)
    assert svc.is_degraded


# ── AC6: batch during fallback ───────────────────────────────────────────────

async def test_ac6_batch_processes_during_fallback():
    """CB open → Whisper batch handles chunk; session not lost."""
    fake_time = [0.0]
    cb = _fake_cb(fake_time=fake_time)
    # Manually open the CB
    for _ in range(CB_FAILURE_THRESHOLD):
        cb.record_failure()
    assert cb.is_open

    batch = FakeBatchBackend(result_text="batch during fallback")
    finals: list = []
    svc = _service(cb=cb, batch=batch)
    await svc.process_chunk(b"audio", speaker="doctor", on_final=finals.append)

    assert batch.call_count == 1
    assert finals[0].text == "batch during fallback"


# ── AC7: circuit breaker closes after health probes ──────────────────────────

async def test_ac7_cb_closes_after_health_probes():
    """5 OK health probes after ≥ 60 s → CB closes + WS event emitted."""
    fake_time = [0.0]
    cb = _fake_cb(fake_time=fake_time)
    for _ in range(CB_FAILURE_THRESHOLD):
        cb.record_failure()
    assert cb.is_open

    ws_events: list[dict] = []
    streaming = FakeStreamingBackend()  # health_ok=True by default
    svc = _service(streaming=streaming, cb=cb, ws_status_events=ws_events)

    # Simulate 61 s elapsed (past minimum open duration)
    fake_time[0] = CB_MIN_OPEN_DURATION_S + 1.0

    # 4 probes — CB should stay open
    for _ in range(CB_HEALTH_THRESHOLD - 1):
        await svc.run_health_probe()
    assert svc.is_degraded

    # 5th probe — CB should close
    await svc.run_health_probe()
    assert not svc.is_degraded
    assert any(e.get("stt_mode") == "streaming" for e in ws_events), ws_events


async def test_ac7_cb_does_not_close_before_min_duration():
    """Health probes before 60 s minimum → CB stays open."""
    fake_time = [0.0]
    cb = _fake_cb(fake_time=fake_time)
    for _ in range(CB_FAILURE_THRESHOLD):
        cb.record_failure()

    svc = _service(
        streaming=FakeStreamingBackend(),
        cb=cb,
    )
    # Time still at 0 — below 60 s minimum
    for _ in range(CB_HEALTH_THRESHOLD):
        await svc.run_health_probe()

    # CB must remain open (min duration not elapsed)
    assert svc.is_degraded


# ── AC8: medical profile — no partials by default ────────────────────────────

async def test_ac8_medical_no_partials_by_default():
    """medical + MEDICAL_STT_PARTIAL_ENABLED=False → on_partial never called."""
    streaming = FakeStreamingBackend()  # calls on_partial if not suppressed
    partials: list = []
    finals: list = []
    svc = _service(streaming=streaming, is_medical=True, medical_partial=False)

    await svc.process_chunk(
        b"audio",
        on_partial=partials.append,
        on_final=finals.append,
    )

    assert partials == [], f"Expected no partials; got {len(partials)}"
    assert len(finals) == 1  # final still delivered


async def test_ac8_medical_partials_when_flag_enabled():
    """medical + medical_partial_enabled=True → on_partial IS called."""
    streaming = FakeStreamingBackend()
    partials: list = []
    svc = _service(streaming=streaming, is_medical=True, medical_partial=True)

    await svc.process_chunk(
        b"audio",
        on_partial=partials.append,
        on_final=lambda _: None,
    )

    assert len(partials) == 1


# ── AC9: [incerto] prefix ─────────────────────────────────────────────────────

def test_ac9_apply_incerto_prefix_adds_prefix():
    """apply_incerto_prefix prepends [incerto] when uncertain=True."""
    result = apply_incerto_prefix("dor no peito")
    assert result == "[incerto] dor no peito"


def test_ac9_apply_incerto_prefix_idempotent():
    """apply_incerto_prefix is idempotent — no double prefix."""
    already = "[incerto] dor no peito"
    assert apply_incerto_prefix(already) == already


async def test_ac9_uncertain_event_text_available_for_prefix():
    """Segment with confidence below threshold → uncertain=True, caller can prefix."""
    finals: list = []
    svc = _service(
        streaming=FakeStreamingBackend(result_confidence=0.30, result_text="dor forte"),
        confidence=0.45,
    )
    await svc.process_chunk(b"audio", on_final=finals.append)

    event = finals[0]
    assert event.uncertain is True
    prefixed = apply_incerto_prefix(event.text)
    assert prefixed.startswith("[incerto]")


# ── AC10: legacy STTService coexists ─────────────────────────────────────────

def test_ac10_legacy_stt_transcribe_coexists():
    """MVP STTService.transcribe() is importable and functional alongside new service."""
    from assistente_voz.stt import STTService, TranscriptionStatus

    class _FakeBackend:
        def transcribe(self, audio, language="pt", sample_rate=None):
            return ("texto legado", 0.95)

    svc = STTService(backend=_FakeBackend())
    outcome = svc.transcribe(b"\x00" * 32, sample_rate=16000)
    assert outcome.status == TranscriptionStatus.OK
    assert outcome.result is not None
    assert outcome.result.text == "texto legado"


# ── GAP-TTL1: close() sets stream TTL ────────────────────────────────────────

async def test_gap_ttl1_close_sets_stream_ttl():
    """AudioGateway.close() calls expire() with STREAM_TTL_SECONDS on the stream key."""
    stream = FakeEventStream()
    gw = AudioGateway(
        session_id=str(uuid.uuid4()),
        tenant_id=DEFAULT_TENANT_ID,
        speaker="doctor",
        stream_client=stream,
        decoder=PassthroughDecoder(),
    )

    # Write at least one event so the key exists
    await gw.process_frame(seq=1, frame_data=b"frame", timestamp_ms=1000)

    events_key = session_events_key(DEFAULT_TENANT_ID, gw.session_id)
    assert stream.ttl_for(events_key) is None  # no TTL yet

    await gw.close()

    assert stream.ttl_for(events_key) == STREAM_TTL_SECONDS


# ── GAP-SAD1: abrupt WS disconnect ───────────────────────────────────────────

async def test_gap_sad1_abrupt_disconnect_unregisters_from_drain(
    db_engine: AsyncEngine,
    app,
    truncate_tables,
) -> None:
    """WS disconnect: no unhandled exception; DrainManager unregisters connection."""
    from datetime import datetime, timedelta, timezone

    from starlette.testclient import TestClient as SyncTestClient

    from assistente_voz.server.db.models import Session as SessionModel

    session_id = uuid.uuid4()
    ws_token_val = str(uuid.uuid4())

    # Insert a minimal open session directly into DB
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as db:
        db.add(
            SessionModel(
                id=session_id,
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                session_kind="medical",
                actor_role="doctor",
                plan="basico",
                plan_features={"stt_mode": "batch", "llm_realtime_red_flag": False},
                idempotency_key=str(uuid.uuid4()),
                status="open",
                ws_token=ws_token_val,
                auto_finalize_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await db.commit()

    with SyncTestClient(app) as tc:
        dm = get_drain_manager()

        with tc.websocket_connect(
            f"/v1/sessions/{session_id}/audio"
            f"?ws_token={ws_token_val}&speaker=doctor",
            headers={"X-Tenant-Id": DEFAULT_TENANT_ID},
        ):
            # Connection is open; at least one conn_id is registered
            assert len(dm._connections) >= 1

        # After context exit the WS is closed; router's finally block unregistered it
        assert len(dm._connections) == 0


# ── GAP-WS1: overflow via TestClient ─────────────────────────────────────────

async def test_gap_ws1_overflow_via_testclient(
    app,
    truncate_tables,
) -> None:
    """Full WS flow via starlette TestClient: connect → 51 frames → STT_OVERFLOW."""
    from starlette.testclient import TestClient as SyncTestClient

    b64_frame = base64.b64encode(b"\x00" * 160).decode()

    with SyncTestClient(app) as tc:
        # Create session via HTTP (TestClient's event loop, same DB override)
        resp = tc.post(
            "/v1/sessions",
            json={
                "session_kind": "medical",
                "actor_role": "doctor",
                "seniority": None,
                "plan": "basico",
            },
            headers={
                "Authorization": "Bearer test",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        session_id = body["session_id"]
        ws_token = body["ws_token"]

        overflow_msg: dict = {}
        with tc.websocket_connect(
            f"/v1/sessions/{session_id}/audio"
            f"?ws_token={ws_token}&speaker=doctor",
            headers={"X-Tenant-Id": DEFAULT_TENANT_ID},
        ) as ws:
            # Send QUEUE_MAX_FRAMES + 1 frames to trigger overflow
            for i in range(QUEUE_MAX_FRAMES + 1):
                ws.send_text(
                    json.dumps(
                        {
                            "seq": i,
                            "data": b64_frame,
                            "timestamp_ms": i * 20,
                        }
                    )
                )
            overflow_msg = ws.receive_json()

    assert overflow_msg.get("type") == "status"
    assert overflow_msg.get("reason") == "STT_OVERFLOW"


# ── GAP-WS2: reconnect handshake via TestClient ──────────────────────────────

async def test_gap_ws2_reconnect_handshake_via_testclient(
    app,
    truncate_tables,
) -> None:
    """Reconnect handshake via starlette TestClient: send {"type":"reconnect"} →
    receive {"type":"session_resumed","last_seq":N}."""
    from starlette.testclient import TestClient as SyncTestClient

    with SyncTestClient(app) as tc:
        resp = tc.post(
            "/v1/sessions",
            json={
                "session_kind": "general_interviewer",
                "actor_role": "doctor",
                "seniority": "senior",
                "plan": "basico",
            },
            headers={
                "Authorization": "Bearer test",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        session_id = body["session_id"]
        ws_token = body["ws_token"]

        handshake: dict = {}
        with tc.websocket_connect(
            f"/v1/sessions/{session_id}/audio"
            f"?ws_token={ws_token}&speaker=doctor",
            headers={"X-Tenant-Id": DEFAULT_TENANT_ID},
        ) as ws:
            ws.send_text(json.dumps({"type": "reconnect"}))
            handshake = ws.receive_json()

    assert handshake.get("type") == "session_resumed"
    assert "last_seq" in handshake
    assert "last_event_id" in handshake
