"""Issue #14 — Pipeline STT real no AudioGateway (tracer bullet sem providers).

AC1  — ``SessionTranscriber`` fecha segmento por janela cheia e chama o STT uma vez
AC2  — Segmento só com silêncio é descartado sem chamar o STT (sem alucinação)
AC3  — Evento final vira entrada ``type=transcript`` no stream e mensagem WS ``transcript``
AC4  — ``uncertain`` propaga do limiar ``STT_CONFIDENCE`` para stream e WS
AC5  — Red flag em tempo real: ``{"type":"red_flag","terms":[...]}`` quando habilitado
AC6  — ``build_stt_service``: off → None; fake → serviço; valor inválido → ValueError
AC7  — ``LocalWhisperBatchBackend`` roda o backend em thread e devolve ``STTEvent``
AC8  — ``BatchOnlyStreamingBackend`` serve o protocolo streaming a partir do batch
AC9  — ``build_transcript`` ordena por ``server_timestamp_ms`` e aplica ``[incerto]``
AC10 — Materialização produz ``transcript`` e ``red_flags`` para ``medical``
AC11 — Dedupe: frame reenviado (mesmo seq/speaker) é ignorado; ``event_id`` determinístico
AC12 — Fluxo ponta a ponta via TestClient com ``STT_SERVER_BACKEND=fake``:
        frames com fala → ``transcript`` no WS → close → draft ``ready`` com o texto
AC13 — ``aclose`` descarrega o segmento final após desconexão abrupta
"""
from __future__ import annotations

import base64
import json
import struct
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.medical_rules import detect_red_flags
from assistente_voz.server.audio_gateway import AudioGateway, PassthroughDecoder
from assistente_voz.server.config import DEFAULT_TENANT_ID, reset_settings_cache
from assistente_voz.server.event_stream import FakeEventStream
from assistente_voz.server.materialization import (
    GENERATION_TRANSCRIPT_ONLY,
    MaterializationWorker,
    build_transcript,
)
from assistente_voz.server.repositories.drafts_repo import DraftsRepository
from assistente_voz.server.stream_client import override_stream_client, reset_stream_client
from assistente_voz.server.stt_service import (
    STTEvent,
    BatchOnlyStreamingBackend,
    FakeBatchBackend,
    FakeStreamingBackend,
    LocalWhisperBatchBackend,
    STTStreamService,
    build_stt_service,
)
from assistente_voz.server.transcriber import SessionTranscriber

SR = 16000
FRAME_SAMPLES = 320  # 20 ms


def _pcm(amplitude: int, samples: int = FRAME_SAMPLES) -> bytes:
    """Square-ish PCM16 frame with the given amplitude (0 → digital silence)."""
    return struct.pack(f"<{samples}h", *([amplitude, -amplitude] * (samples // 2)))


SPEECH = _pcm(8000)
SILENCE = _pcm(0)


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, message: dict) -> None:
        self.messages.append(message)


def _gateway(stream: FakeEventStream | None = None, *, is_medical: bool = False) -> AudioGateway:
    return AudioGateway(
        session_id=str(uuid.uuid4()),
        tenant_id=DEFAULT_TENANT_ID,
        speaker="doctor",
        stream_client=stream or FakeEventStream(),
        decoder=PassthroughDecoder(),
        is_medical=is_medical,
    )


def _service(**kwargs) -> STTStreamService:
    streaming = kwargs.pop("streaming", None) or FakeStreamingBackend(**kwargs.pop("fake", {}))
    return STTStreamService(
        streaming,
        FakeBatchBackend(),
        confidence_threshold=kwargs.pop("confidence", 0.55),
        is_medical=kwargs.pop("is_medical", False),
        _timeout_s=5.0,
    )


async def _feed(gw: AudioGateway, tr: SessionTranscriber, frames: list[bytes], start_seq: int = 0):
    for i, data in enumerate(frames):
        result = await gw.process_frame(seq=start_seq + i, frame_data=data, timestamp_ms=1_000_000 + i * 20)
        assert not result.duplicate
        frame = gw._queue.get_nowait()  # noqa: SLF001 — drive the consumer inline
        await tr.handle_frame(frame)


# ── AC1 / AC2 — segmentation ─────────────────────────────────────────────────

async def test_ac1_window_full_dispatches_single_stt_call():
    stream = FakeEventStream()
    gw = _gateway(stream)
    streaming = FakeStreamingBackend(result_text="dor de cabeça")
    stt = _service(streaming=streaming)
    out = _Collector()
    tr = SessionTranscriber(gateway=gw, stt=stt, speaker="doctor", send=out.send, window_s=1.0)

    await _feed(gw, tr, [SPEECH] * 50)  # exactly 1 s
    await tr.aclose()

    assert streaming.stream_call_count == 1
    assert tr.segments_sent == 1


async def test_ac2_silence_only_segment_never_calls_stt():
    stream = FakeEventStream()
    gw = _gateway(stream)
    streaming = FakeStreamingBackend()
    stt = _service(streaming=streaming)
    out = _Collector()
    tr = SessionTranscriber(gateway=gw, stt=stt, speaker="doctor", send=out.send, window_s=0.5)

    await _feed(gw, tr, [SILENCE] * 60)
    await tr.aclose()

    assert streaming.stream_call_count == 0
    assert tr.segments_discarded >= 1
    assert [m for m in out.messages if m["type"] == "transcript"] == []


# ── AC3 / AC4 — transcript entry + WS message ────────────────────────────────

async def test_ac3_final_event_recorded_in_stream_and_sent_to_ws():
    stream = FakeEventStream()
    gw = _gateway(stream)
    stt = _service(fake={"result_text": "estou com dor de cabeça", "result_confidence": 0.9})
    out = _Collector()
    tr = SessionTranscriber(gateway=gw, stt=stt, speaker="doctor", send=out.send, window_s=0.5)

    await _feed(gw, tr, [SPEECH] * 25)  # exactly one 0.5 s window
    await tr.aclose()

    transcripts = [f for _, f in stream.stream_entries(gw._events_key()) if f.get("type") == "transcript"]
    assert len(transcripts) == 1
    assert transcripts[0]["text"] == "estou com dor de cabeça"
    assert transcripts[0]["speaker"] == "doctor"
    assert transcripts[0]["uncertain"] == "false"

    ws = [m for m in out.messages if m["type"] == "transcript"]
    assert len(ws) == 1
    assert ws[0]["text"] == "estou com dor de cabeça"
    assert ws[0]["is_final"] is True
    assert ws[0]["seq_from"] == 0 and ws[0]["seq_to"] == 24


async def test_ac4_low_confidence_marks_uncertain_everywhere():
    stream = FakeEventStream()
    gw = _gateway(stream)
    stt = _service(fake={"result_text": "há três dias", "result_confidence": 0.40}, confidence=0.55)
    out = _Collector()
    tr = SessionTranscriber(gateway=gw, stt=stt, speaker="patient", send=out.send, window_s=0.5)
    gw.speaker = "patient"

    await _feed(gw, tr, [SPEECH] * 25)
    await tr.aclose()

    entry = [f for _, f in stream.stream_entries(gw._events_key()) if f.get("type") == "transcript"][0]
    assert entry["uncertain"] == "true"
    assert [m for m in out.messages if m["type"] == "transcript"][0]["uncertain"] is True


# ── AC5 — realtime red flags ─────────────────────────────────────────────────

async def test_ac5_red_flag_emitted_when_enabled():
    stream = FakeEventStream()
    gw = _gateway(stream, is_medical=True)
    stt = _service(fake={"result_text": "sinto falta de ar e dor no peito"}, is_medical=True)
    out = _Collector()
    tr = SessionTranscriber(
        gateway=gw, stt=stt, speaker="patient", send=out.send, window_s=0.5,
        realtime_red_flags=True, red_flag_detector=detect_red_flags,
    )

    await _feed(gw, tr, [SPEECH] * 25)
    await tr.aclose()

    flags = [m for m in out.messages if m["type"] == "red_flag"]
    assert len(flags) == 1
    assert flags[0]["terms"] == ["dor no peito", "falta de ar"]
    entry = [f for _, f in stream.stream_entries(gw._events_key()) if f.get("type") == "transcript"][0]
    assert entry["red_flags"] == "dor no peito,falta de ar"


async def test_ac5_no_red_flag_message_when_disabled():
    stream = FakeEventStream()
    gw = _gateway(stream, is_medical=True)
    stt = _service(fake={"result_text": "sinto falta de ar"}, is_medical=True)
    out = _Collector()
    tr = SessionTranscriber(
        gateway=gw, stt=stt, speaker="patient", send=out.send, window_s=0.5,
        realtime_red_flags=False, red_flag_detector=detect_red_flags,
    )
    await _feed(gw, tr, [SPEECH] * 30)
    await tr.aclose()
    assert [m for m in out.messages if m["type"] == "red_flag"] == []


# ── AC6 — factory ────────────────────────────────────────────────────────────

def test_ac6_build_stt_service_off_returns_none():
    assert build_stt_service("off", is_medical=False) is None
    assert build_stt_service("", is_medical=False) is None


def test_ac6_build_stt_service_fake_returns_service(monkeypatch):
    monkeypatch.setenv("STT_CONFIDENCE", "0.45")
    svc = build_stt_service("fake", is_medical=True)
    assert isinstance(svc, STTStreamService)
    assert svc._confidence_threshold == pytest.approx(0.45)  # noqa: SLF001


def test_ac6_build_stt_service_invalid_raises():
    with pytest.raises(ValueError):
        build_stt_service("deepgram-not-yet", is_medical=False)


# ── AC7 / AC8 — local whisper adapter + batch-only streaming ─────────────────

async def test_ac7_local_whisper_backend_runs_in_thread():
    import threading

    seen: dict = {}

    class _FakeWhisper:
        def transcribe(self, audio, language="pt", sample_rate=None):
            seen["thread"] = threading.current_thread().name
            seen["sample_rate"] = sample_rate
            return " losartana 50 mg ", 0.8

    backend = LocalWhisperBatchBackend(backend=_FakeWhisper(), language="pt")
    event = await backend.transcribe_batch(b"\x00" * 64, speaker="doctor", sample_rate=16000)
    assert isinstance(event, STTEvent)
    assert event.text == "losartana 50 mg"
    assert event.confidence == pytest.approx(0.8)
    assert seen["sample_rate"] == 16000
    assert seen["thread"] != threading.main_thread().name, "deve rodar fora do event loop"


async def test_ac8_batch_only_streaming_backend_emits_one_final():
    batch = FakeBatchBackend(result_text="texto batch")
    streaming = BatchOnlyStreamingBackend(batch)
    finals: list[STTEvent] = []

    async def _chunks():
        yield b"a"
        yield b"b"

    await streaming.stream(_chunks(), "doctor", None, finals.append)
    assert len(finals) == 1 and finals[0].text == "texto batch"
    assert await streaming.health_check() is True
    assert batch.call_count == 1


# ── AC9 / AC10 — draft content from the stream ───────────────────────────────

def test_ac9_build_transcript_orders_by_timestamp_and_prefixes_incerto():
    entries = [
        ("1-0", {"type": "frame", "seq": "0"}),
        ("1-1", {"type": "transcript", "speaker": "patient", "text": "dói muito",
                 "confidence": "0.40", "uncertain": "true", "server_timestamp_ms": "2000"}),
        ("1-2", {"type": "transcript", "speaker": "doctor", "text": "onde dói?",
                 "confidence": "0.95", "uncertain": "false", "server_timestamp_ms": "1000"}),
        ("1-3", {"type": "transcript", "speaker": "doctor", "text": "   ",
                 "confidence": "0.9", "uncertain": "false", "server_timestamp_ms": "3000"}),
    ]
    t = build_transcript(entries)
    assert t["frame_count"] == 1
    assert t["segment_count"] == 2
    assert t["uncertain_count"] == 1
    assert [s["text"] for s in t["segments"]] == ["onde dói?", "dói muito"]
    assert t["text"] == "doctor: onde dói?\npatient: [incerto] dói muito"


async def test_ac10_materialization_builds_transcript_and_red_flags(
    db_engine: AsyncEngine, truncate_tables: None
) -> None:
    from assistente_voz.server.repositories.sessions_repo import SessionsRepository

    stream = FakeEventStream()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            session = await SessionsRepository(db, DEFAULT_TENANT_ID).create(
                session_kind="medical", actor_role="doctor", plan="premium", seniority=None,
                plan_features={}, idempotency_key=str(uuid.uuid4()), ws_token=uuid.uuid4(),
            )
            session_id = session.id
            await DraftsRepository(db, DEFAULT_TENANT_ID).insert_pending(session_id)

    gw = AudioGateway(session_id=str(session_id), tenant_id=DEFAULT_TENANT_ID, speaker="patient",
                      stream_client=stream, is_medical=True)
    await gw.process_frame(seq=0, frame_data=SPEECH, timestamp_ms=1_000_000)
    await gw.record_transcript(event_id="e1", text="sinto dispneia ao subir escadas",
                               confidence=0.9, uncertain=False, timestamp_ms=1_000_000)

    worker = MaterializationWorker(tenant_id=DEFAULT_TENANT_ID, session_id=str(session_id),
                                   stream_client=stream, session_kind="medical")
    await worker.run()

    async with sm() as db:
        draft = await DraftsRepository(db, DEFAULT_TENANT_ID).get_by_session(session_id)
    assert draft is not None and draft.draft_status == "ready"
    content = draft.content
    assert content["draft_generation"] == GENERATION_TRANSCRIPT_ONLY
    assert content["transcript"]["text"] == "patient: sinto dispneia ao subir escadas"
    assert content["transcript"]["frame_count"] == 1
    assert content["red_flags"] == ["dispneia"]
    assert content["event_count"] == 2
    assert "events" not in content, "frames brutos não vão para o draft"


# ── AC11 — dedupe ────────────────────────────────────────────────────────────

async def test_ac11_resent_frame_is_deduplicated_with_deterministic_event_id():
    stream = FakeEventStream()
    gw_a = _gateway(stream)
    sid = gw_a.session_id
    r1 = await gw_a.process_frame(seq=7, frame_data=SPEECH, timestamp_ms=1_000_000)
    assert not r1.duplicate

    # Reconnect: fresh gateway on the same stream resends seq 7 then sends seq 8.
    gw_b = AudioGateway(session_id=sid, tenant_id=DEFAULT_TENANT_ID, speaker="doctor", stream_client=stream)
    r2 = await gw_b.process_frame(seq=7, frame_data=SPEECH, timestamp_ms=1_000_020)
    r3 = await gw_b.process_frame(seq=8, frame_data=SPEECH, timestamp_ms=1_000_040)

    assert r2.duplicate and r2.stream_id is None and r2.event_id == r1.event_id
    assert not r3.duplicate
    frames = [f for _, f in stream.stream_entries(gw_a._events_key()) if f.get("type") == "frame"]
    assert [f["seq"] for f in frames] == ["7", "8"]
    assert gw_b.duplicate_count == 1


async def test_ac11_same_seq_other_speaker_is_not_a_duplicate():
    stream = FakeEventStream()
    sid = str(uuid.uuid4())
    doc = AudioGateway(session_id=sid, tenant_id=DEFAULT_TENANT_ID, speaker="doctor", stream_client=stream)
    pat = AudioGateway(session_id=sid, tenant_id=DEFAULT_TENANT_ID, speaker="patient", stream_client=stream)
    r1 = await doc.process_frame(seq=0, frame_data=SPEECH, timestamp_ms=1)
    r2 = await pat.process_frame(seq=0, frame_data=SPEECH, timestamp_ms=1)
    assert not r1.duplicate and not r2.duplicate
    assert r1.event_id != r2.event_id


# ── AC12 — end to end via TestClient ─────────────────────────────────────────

@pytest.mark.usefixtures("truncate_tables")
async def test_ac12_end_to_end_fake_stt_transcript_reaches_draft(app, db_engine, monkeypatch):
    from starlette.testclient import TestClient as SyncTestClient

    monkeypatch.setenv("STT_SERVER_BACKEND", "fake")
    monkeypatch.setenv("STT_WINDOW_S", "0.5")
    monkeypatch.setenv("SESSION_REAPER_INTERVAL_S", "0")
    reset_settings_cache()
    fake_stream = FakeEventStream()
    override_stream_client(fake_stream)
    try:
        with SyncTestClient(app) as tc:
            headers = {
                "Authorization": "Bearer test",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            }
            resp = tc.post(
                "/v1/sessions",
                json={"session_kind": "medical", "actor_role": "doctor", "seniority": None, "plan": "premium"},
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            session_id, ws_token = body["session_id"], body["ws_token"]

            b64_speech = base64.b64encode(SPEECH).decode()
            transcript_msg: dict = {}
            with tc.websocket_connect(
                f"/v1/sessions/{session_id}/audio?ws_token={ws_token}&speaker=doctor"
            ) as ws:
                status_msg = ws.receive_json()
                assert status_msg["type"] == "status" and status_msg["backend"] == "fake"
                for i in range(30):  # 600 ms of "speech" > 0.5 s window
                    ws.send_text(json.dumps({"seq": i, "data": b64_speech, "timestamp_ms": i * 20}))
                for _ in range(10):
                    msg = ws.receive_json()
                    if msg.get("type") == "transcript":
                        transcript_msg = msg
                        break
            assert transcript_msg.get("text") == "texto transcrito"
            assert transcript_msg["speaker"] == "doctor"

            close = tc.post(f"/v1/sessions/{session_id}/close", headers={"Authorization": "Bearer test"})
            assert close.status_code == 200, close.text
            draft = tc.get(f"/v1/sessions/{session_id}/draft", headers={"Authorization": "Bearer test"})
            assert draft.status_code == 200, draft.text
            content = draft.json()["content"]
            assert "texto transcrito" in content["transcript"]["text"]
            assert content["transcript"]["frame_count"] == 30
    finally:
        reset_stream_client()
        reset_settings_cache()


# ── AC13 — trailing segment flushed on close ─────────────────────────────────

async def test_ac13_aclose_flushes_trailing_segment():
    stream = FakeEventStream()
    gw = _gateway(stream)
    streaming = FakeStreamingBackend(result_text="última frase")
    stt = _service(streaming=streaming)
    out = _Collector()
    tr = SessionTranscriber(gateway=gw, stt=stt, speaker="doctor", send=out.send, window_s=10.0)

    await _feed(gw, tr, [SPEECH] * 10)  # far below the window
    assert streaming.stream_call_count == 0
    await tr.aclose()
    assert streaming.stream_call_count == 1
    assert [m["text"] for m in out.messages if m["type"] == "transcript"] == ["última frase"]
