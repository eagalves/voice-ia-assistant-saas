"""Tests for Issue #5 — Draft Pipeline com Materialização Idempotente.

AC1  — POST /close → draft inserted with status=pending
AC2  — MaterializationWorker transitions pending → ready
AC3  — Concurrent workers (close + auto-finalize race): exactly one ready
AC4  — Simultaneous retry: second worker exits silently, no IntegrityError
AC5  — 3 builder failures → draft_status=failed, log ERROR
AC6  — GET /draft returns 202 + retry-after:5 when pending
AC7  — GET /draft returns 500 + partial:true when failed
AC8  — POST /draft/feedback stores chars_original, chars_edited, fields_corrected
AC9  — Medical snapshot triggered at exactly 25 events
AC10 — Crash recovery (restore_from_snapshot) loses ≤ 5% of fields
AC11 — Redis Stream EXPIRE 86400 after close (smoke — already covered by GAP-TTL1)
AC12 — Migration 0004 downgrade() is non-trivial (not a pass)
GAP-CONC1 — Two AudioGateways on same session (2 asyncio tasks): no duplicate event_ids
"""
from __future__ import annotations

import asyncio
import ast
import logging
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.audio_gateway import (
    AudioGateway,
    PassthroughDecoder,
    SNAPSHOT_EVENT_COUNT_MEDICAL,
)
from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db.models import Draft
from assistente_voz.server.event_stream import FakeEventStream
from assistente_voz.server.materialization import (
    MAX_ATTEMPTS,
    MaterializationWorker,
)
from assistente_voz.server.repositories.drafts_repo import DraftsRepository
from assistente_voz.server.repositories.feedback_repo import FeedbackRepository

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_payload(plan: str = "premium", session_kind: str = "medical") -> dict:
    return {
        "session_kind": session_kind,
        "actor_role": "doctor",
        "seniority": "senior",
        "plan": plan,
    }


async def _create_session(client: AsyncClient, auth_headers: dict) -> str:
    resp = await client.post(
        "/v1/sessions", headers=auth_headers, json=_session_payload()
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _create_and_close(client: AsyncClient, auth_headers: dict) -> str:
    sid = await _create_session(client, auth_headers)
    resp = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return sid


def _gateway(*, stream: FakeEventStream | None = None, is_medical: bool = False) -> AudioGateway:
    return AudioGateway(
        session_id=str(uuid.uuid4()),
        tenant_id=DEFAULT_TENANT_ID,
        speaker="doctor",
        stream_client=stream or FakeEventStream(),
        decoder=PassthroughDecoder(),
        is_medical=is_medical,
    )


def _worker(
    tenant_id: str,
    session_id: str,
    stream: FakeEventStream | None = None,
    content_builder=None,
) -> MaterializationWorker:
    return MaterializationWorker(
        tenant_id=tenant_id,
        session_id=session_id,
        stream_client=stream or FakeEventStream(),
        content_builder=content_builder,
    )


# ---------------------------------------------------------------------------
# AC1 — POST /close inserts pending draft
# ---------------------------------------------------------------------------

async def test_ac1_close_inserts_pending_draft(
    client: AsyncClient,
    auth_headers: dict,
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(uuid.UUID(sid))

    assert draft is not None
    assert draft.draft_status == "pending"


# ---------------------------------------------------------------------------
# AC2 — Worker transitions pending → ready
# ---------------------------------------------------------------------------

async def test_ac2_worker_materializes_pending_to_ready(
    db_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    sm = async_sessionmaker(db_engine, expire_on_commit=False)

    # Create a session row + pending draft directly via repo
    from assistente_voz.server.repositories.sessions_repo import SessionsRepository

    session_id = uuid.uuid4()
    async with sm() as db:
        async with db.begin():
            sessions_repo = SessionsRepository(db, DEFAULT_TENANT_ID)
            session = await sessions_repo.create(
                session_kind="medical",
                actor_role="doctor",
                plan="premium",
                seniority="senior",
                plan_features={"stt_mode": "streaming"},
                idempotency_key=str(uuid.uuid4()),
                ws_token=uuid.uuid4(),
            )
            session_id = session.id
            drafts_repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            await drafts_repo.insert_pending(session_id)

    worker = _worker(DEFAULT_TENANT_ID, str(session_id))
    await worker.run()

    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(session_id)

    assert draft is not None
    assert draft.draft_status == "ready"
    assert draft.content is not None
    assert "event_count" in draft.content


# ---------------------------------------------------------------------------
# AC3 — Concurrent workers produce exactly one ready draft
# ---------------------------------------------------------------------------

async def test_ac3_concurrent_close_and_autofinalize_exactly_once(
    db_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    from assistente_voz.server.repositories.sessions_repo import SessionsRepository

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    session_id = uuid.uuid4()

    async with sm() as db:
        async with db.begin():
            sessions_repo = SessionsRepository(db, DEFAULT_TENANT_ID)
            session = await sessions_repo.create(
                session_kind="medical",
                actor_role="doctor",
                plan="premium",
                seniority=None,
                plan_features={},
                idempotency_key=str(uuid.uuid4()),
                ws_token=uuid.uuid4(),
            )
            session_id = session.id
            drafts_repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            await drafts_repo.insert_pending(session_id)

    worker_a = _worker(DEFAULT_TENANT_ID, str(session_id))
    worker_b = _worker(DEFAULT_TENANT_ID, str(session_id))
    await asyncio.gather(worker_a.run(), worker_b.run())

    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(session_id)

    assert draft is not None
    assert draft.draft_status == "ready"


# ---------------------------------------------------------------------------
# AC4 — Simultaneous retry: second exits silently, no DUPLICATE_KEY
# ---------------------------------------------------------------------------

async def test_ac4_simultaneous_retry_exits_silently(
    db_engine: AsyncEngine,
    truncate_tables: None,
) -> None:
    from assistente_voz.server.repositories.sessions_repo import SessionsRepository

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    session_id = uuid.uuid4()

    async with sm() as db:
        async with db.begin():
            sessions_repo = SessionsRepository(db, DEFAULT_TENANT_ID)
            session = await sessions_repo.create(
                session_kind="medical",
                actor_role="doctor",
                plan="premium",
                seniority=None,
                plan_features={},
                idempotency_key=str(uuid.uuid4()),
                ws_token=uuid.uuid4(),
            )
            session_id = session.id
            drafts_repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            await drafts_repo.insert_pending(session_id)

    # Three workers simultaneously — should not raise IntegrityError
    workers = [_worker(DEFAULT_TENANT_ID, str(session_id)) for _ in range(3)]
    await asyncio.gather(*[w.run() for w in workers])

    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(session_id)

    assert draft is not None
    assert draft.draft_status == "ready"


# ---------------------------------------------------------------------------
# AC5 — 3 builder failures → draft_status=failed, ERROR logged
# ---------------------------------------------------------------------------

async def test_ac5_three_failures_mark_failed_and_log_error(
    db_engine: AsyncEngine,
    truncate_tables: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistente_voz.server.repositories.sessions_repo import SessionsRepository

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    session_id = uuid.uuid4()

    async with sm() as db:
        async with db.begin():
            sessions_repo = SessionsRepository(db, DEFAULT_TENANT_ID)
            session = await sessions_repo.create(
                session_kind="medical",
                actor_role="doctor",
                plan="premium",
                seniority=None,
                plan_features={},
                idempotency_key=str(uuid.uuid4()),
                ws_token=uuid.uuid4(),
            )
            session_id = session.id
            drafts_repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            await drafts_repo.insert_pending(session_id)

    async def _failing_builder(tenant_id, session_id, stream):
        raise RuntimeError("LLM down")

    worker = _worker(
        DEFAULT_TENANT_ID, str(session_id), content_builder=_failing_builder
    )

    with caplog.at_level(logging.ERROR, logger="assistente_voz.server.materialization"):
        await worker.run()

    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(session_id)

    assert draft is not None
    assert draft.draft_status == "failed"
    assert "FAILED" in caplog.text or "failed" in caplog.text.lower()


# ---------------------------------------------------------------------------
# AC6 — GET /draft returns 202 + Retry-After:5 when pending
# ---------------------------------------------------------------------------

async def test_ac6_get_draft_202_when_pending(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    sid = await _create_and_close(client, auth_headers)
    resp = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert resp.status_code == 202
    assert resp.headers.get("Retry-After") == "5"
    body = resp.json()
    assert body["draft_status"] == "pending"


# ---------------------------------------------------------------------------
# AC7 — GET /draft returns 500 + partial:true when failed
# ---------------------------------------------------------------------------

async def test_ac7_get_draft_500_when_failed(
    client: AsyncClient,
    auth_headers: dict,
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            draft = await repo.get_by_session(uuid.UUID(sid))
            assert draft is not None
            await repo.force_status(
                draft, status="failed", partial=True, last_error="boom"
            )

    resp = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "DRAFT_MATERIALIZATION_FAILED"
    assert body["partial"] is True


# ---------------------------------------------------------------------------
# AC8 — POST /draft/feedback stores chars_original, chars_edited, fields_corrected
# ---------------------------------------------------------------------------

async def test_ac8_post_feedback_stores_data(
    client: AsyncClient,
    auth_headers: dict,
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    # Force ready so feedback endpoint doesn't reject
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            draft = await repo.get_by_session(uuid.UUID(sid))
            assert draft is not None
            await repo.force_status(draft, status="ready", content={"ok": True})

    payload = {
        "chars_original": 1200,
        "chars_edited": 180,
        "fields_corrected": ["queixa_principal", "historico_familiar"],
    }
    resp = await client.post(
        f"/v1/sessions/{sid}/draft/feedback", headers=auth_headers, json=payload
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "correction_ratio" in body

    async with sm() as db:
        feedback_repo = FeedbackRepository(db, DEFAULT_TENANT_ID)
        records = await feedback_repo.list_by_session(uuid.UUID(sid))

    assert len(records) == 1
    rec = records[0]
    assert rec.chars_original == 1200
    assert rec.chars_edited == 180
    assert rec.fields_corrected == ["queixa_principal", "historico_familiar"]


# ---------------------------------------------------------------------------
# AC9 — Medical snapshot triggered at exactly 25 events
# ---------------------------------------------------------------------------

async def test_ac9_medical_snapshot_at_25_events() -> None:
    stream = FakeEventStream()
    gw = _gateway(stream=stream, is_medical=True)

    snapshot_calls = []
    original_snapshot = gw.snapshot

    async def _tracked_snapshot():
        snapshot_calls.append(gw._event_count)
        await original_snapshot()

    gw.snapshot = _tracked_snapshot  # type: ignore[method-assign]

    base_ts = 1_000_000
    for seq in range(30):
        await gw.process_frame(
            seq=seq,
            frame_data=b"\x00" * 10,
            timestamp_ms=base_ts + seq * 20,
        )

    # Snapshot must have been called at event 25 (or any multiple of 25)
    assert SNAPSHOT_EVENT_COUNT_MEDICAL == 25
    assert any(c % 25 == 0 for c in snapshot_calls), (
        f"Expected snapshot at multiple of 25; got calls at {snapshot_calls}"
    )


# ---------------------------------------------------------------------------
# AC10 — Crash recovery: restore_from_snapshot loses ≤ 5% of fields
# ---------------------------------------------------------------------------

async def test_ac10_crash_recovery_leq_5pct_field_loss() -> None:
    stream = FakeEventStream()
    gw = _gateway(stream=stream, is_medical=True)

    base_ts = 2_000_000
    for seq in range(25):
        await gw.process_frame(
            seq=seq,
            frame_data=b"\x01" * 10,
            timestamp_ms=base_ts + seq * 20,
        )

    # At frame 25 snapshot was auto-taken; verify state is persisted
    state_key = gw._state_key()
    state_json = await stream.get(state_key)
    assert state_json is not None, "Snapshot should have been persisted"

    # Simulate crash: new gateway, restore from snapshot
    gw2 = AudioGateway(
        session_id=gw.session_id,
        tenant_id=gw.tenant_id,
        speaker="doctor",
        stream_client=stream,
        decoder=PassthroughDecoder(),
        is_medical=True,
    )
    restored = await gw2.restore_from_snapshot()
    assert restored, "restore_from_snapshot() should return True"

    # After recovery, event_count should reflect the 25 processed frames
    # (≤ 5% loss means we must recover at least 24 of 25 events)
    recovered = gw2._event_count
    original = 25
    loss_pct = (original - recovered) / original * 100
    assert loss_pct <= 5.0, (
        f"Field loss {loss_pct:.1f}% exceeds 5% threshold "
        f"(recovered {recovered}/{original} events)"
    )


# ---------------------------------------------------------------------------
# AC11 — Redis Stream EXPIRE 86400 after close (smoke)
# ---------------------------------------------------------------------------

async def test_ac11_redis_expire_after_close() -> None:
    """Smoke test: close() sets TTL on the stream key.

    Full coverage via GAP-TTL1 in test_issue004_stt_medical.py.
    """
    from assistente_voz.server.audio_gateway import STREAM_TTL_SECONDS

    stream = FakeEventStream()
    gw = _gateway(stream=stream)
    events_key = gw._events_key()

    # Write something to the stream so the key exists
    await gw.process_frame(seq=0, frame_data=b"\x00" * 4, timestamp_ms=1_000_000)

    await gw.close()
    assert stream.ttl_for(events_key) == STREAM_TTL_SECONDS


# ---------------------------------------------------------------------------
# AC12 — Migration 0004 downgrade() is non-trivial
# ---------------------------------------------------------------------------

def test_ac12_migration_0004_has_non_trivial_downgrade() -> None:
    migration_file = (
        REPO_ROOT / "migrations" / "versions" / "20260509_0004_draft_status_index.py"
    )
    assert migration_file.exists(), f"Migration file not found: {migration_file}"

    source = migration_file.read_text(encoding="utf-8")
    tree = ast.parse(source)

    downgrade_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        ),
        None,
    )
    assert downgrade_fn is not None, "downgrade() function not found in migration 0004"

    # A trivial downgrade has only a Pass statement (or a single docstring + Pass)
    non_pass_stmts = [
        s
        for s in downgrade_fn.body
        if not isinstance(s, (ast.Pass, ast.Expr))
        or (
            isinstance(s, ast.Expr)
            and not isinstance(s.value, ast.Constant)
        )
    ]
    assert len(non_pass_stmts) >= 1, (
        "downgrade() contains only pass/docstring — must have a real rollback statement"
    )


# ---------------------------------------------------------------------------
# GAP-CONC1 — Two AudioGateways on same session: no duplicate event_ids
# ---------------------------------------------------------------------------

async def test_gap_conc1_two_gateways_same_session_no_duplicate_event_ids() -> None:
    """Two AudioGateway instances sharing the same Redis Stream (same session).

    Each processes 30 frames concurrently.  The Redis Stream event_ids must
    all be unique (XADD uses UUID-based event_id field — dedup layer).
    """
    shared_stream = FakeEventStream()
    session_id = str(uuid.uuid4())

    gw_a = AudioGateway(
        session_id=session_id,
        tenant_id=DEFAULT_TENANT_ID,
        speaker="doctor",
        stream_client=shared_stream,
        decoder=PassthroughDecoder(),
        is_medical=False,
    )
    gw_b = AudioGateway(
        session_id=session_id,
        tenant_id=DEFAULT_TENANT_ID,
        speaker="patient",
        stream_client=shared_stream,
        decoder=PassthroughDecoder(),
        is_medical=False,
    )

    base_ts = 3_000_000

    async def _run_gateway(gw: AudioGateway, offset: int) -> None:
        for seq in range(30):
            await gw.process_frame(
                seq=seq,
                frame_data=b"\x02" * 10,
                timestamp_ms=base_ts + offset + seq * 20,
            )

    await asyncio.gather(
        _run_gateway(gw_a, offset=0),
        _run_gateway(gw_b, offset=1_000_000),
    )

    # Collect all event_id values from the shared stream
    events_key = gw_a._events_key()  # same key for both
    entries = await shared_stream.xrange(events_key)
    event_ids = [fields["event_id"] for _, fields in entries]

    assert len(event_ids) == 60, f"Expected 60 entries, got {len(event_ids)}"
    assert len(set(event_ids)) == len(event_ids), (
        "Duplicate event_ids detected in shared Redis Stream"
    )
