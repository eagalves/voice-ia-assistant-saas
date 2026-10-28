from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.config import DEFAULT_TENANT_ID
from assistente_voz.server.db.models import Draft, DraftFeedback
from assistente_voz.server.repositories.drafts_repo import DraftsRepository


def _payload() -> dict:
    return {
        "session_kind": "medical",
        "actor_role": "doctor",
        "seniority": "senior",
        "plan": "premium",
    }


async def _create_and_close(
    client: AsyncClient, auth_headers: dict[str, str]
) -> str:
    create = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    sid = create.json()["session_id"]
    close = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert close.status_code == 200
    return sid


async def _force_status(db_engine: AsyncEngine, sid: str, status: str) -> None:
    """The materialization worker runs as a BackgroundTask and finishes before
    the HTTP response is returned, so drafts are ``ready`` right after close.
    Tests that need another state set it explicitly."""
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(uuid.UUID(sid))
        assert draft is not None
        await repo.force_status(draft, status=status)
        await db.commit()


@pytest.mark.asyncio
async def test_get_draft_pending_returns_202_with_retry_after(
    client: AsyncClient, auth_headers: dict[str, str], db_engine: AsyncEngine
) -> None:
    sid = await _create_and_close(client, auth_headers)
    await _force_status(db_engine, sid, "pending")
    resp = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert resp.status_code == 202
    assert resp.headers.get("Retry-After") == "5"
    body = resp.json()
    assert body["draft_status"] == "pending"
    assert body["error"] == "DRAFT_NOT_READY"


@pytest.mark.asyncio
async def test_get_draft_ready_returns_200(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(uuid.UUID(sid))
        assert draft is not None
        await repo.force_status(
            draft, status="ready", content={"queixa": "ok"}, partial=False
        )
        await db.commit()

    resp = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_status"] == "ready"
    assert body["content"] == {"queixa": "ok"}


@pytest.mark.asyncio
async def test_get_draft_failed_returns_500_with_partial(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(uuid.UUID(sid))
        assert draft is not None
        await repo.force_status(
            draft,
            status="failed",
            content={"campo": "valor parcial"},
            partial=True,
            last_error="LLM_ERR_UNAVAILABLE",
        )
        await db.commit()

    resp = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "DRAFT_MATERIALIZATION_FAILED"
    assert body["partial"] is True
    assert body["content"] == {"campo": "valor parcial"}
    assert body["last_error"] == "LLM_ERR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_draft_retry_resets_failed_to_pending(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        repo = DraftsRepository(db, DEFAULT_TENANT_ID)
        draft = await repo.get_by_session(uuid.UUID(sid))
        await repo.force_status(draft, status="failed", last_error="boom")
        await db.commit()

    resp = await client.post(
        f"/v1/sessions/{sid}/draft/retry", headers=auth_headers
    )
    assert resp.status_code == 202
    assert resp.json()["draft_status"] == "pending"
    assert resp.json()["retry_count"] == 1

    async with sm() as db:
        row = (
            await db.execute(
                select(Draft).where(Draft.session_id == uuid.UUID(sid))
            )
        ).scalar_one()
        # retry resets to pending AND reschedules the worker, which completes
        # before the response (BackgroundTask) → ready with no error left.
        assert row.draft_status == "ready"
        assert row.last_error is None
        assert row.retry_count == 1


@pytest.mark.asyncio
async def test_draft_retry_invalid_state_returns_409(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    sid = await _create_and_close(client, auth_headers)
    resp = await client.post(
        f"/v1/sessions/{sid}/draft/retry", headers=auth_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "DRAFT_RETRY_INVALID_STATE"


@pytest.mark.asyncio
async def test_draft_feedback_persists_row(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    sid = await _create_and_close(client, auth_headers)
    body = {
        "chars_original": 1200,
        "chars_edited": 240,
        "fields_corrected": ["queixa_principal", "historico_familiar"],
        "actor_sub": "doctor-123",
    }
    resp = await client.post(
        f"/v1/sessions/{sid}/draft/feedback",
        headers=auth_headers,
        json=body,
    )
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["correction_ratio"] == pytest.approx(0.2, abs=1e-4)

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        rows = (
            await db.execute(
                select(DraftFeedback).where(
                    DraftFeedback.session_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].chars_edited == 240
    assert rows[0].fields_corrected == ["queixa_principal", "historico_familiar"]


@pytest.mark.asyncio
async def test_get_draft_session_not_found(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get(
        f"/v1/sessions/{uuid.uuid4()}/draft", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SESSION_NOT_FOUND"
