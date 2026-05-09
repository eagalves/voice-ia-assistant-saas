from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from assistente_voz.server.db.models import Draft, Session


def _payload(plan: str = "premium", session_kind: str = "medical") -> dict:
    return {
        "session_kind": session_kind,
        "actor_role": "doctor",
        "seniority": "senior",
        "plan": plan,
    }


@pytest.mark.asyncio
async def test_create_session_201_full_payload(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert {"session_id", "ws_url", "ws_token", "session_kind", "plan_features"}.issubset(
        body.keys()
    )
    uuid.UUID(body["session_id"])
    uuid.UUID(body["ws_token"])
    assert body["session_kind"] == "medical"
    assert body["ws_url"].startswith("ws")
    assert body["ws_url"].endswith(f"/v1/sessions/{body['session_id']}/audio")
    assert body["plan_features"]["session_max_minutes"] == 240
    assert body["plan_features"]["stt_mode"] == "streaming"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan,expected_max,expected_stt,expected_model,expected_realtime",
    [
        ("basico", 60, "batch", "gpt-4o-mini", False),
        ("medio", 60, "streaming", "gpt-4o-mini", True),
        ("premium", 240, "streaming", "gpt-4o", True),
    ],
)
async def test_plan_features_per_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    plan: str,
    expected_max: int,
    expected_stt: str,
    expected_model: str,
    expected_realtime: bool,
) -> None:
    resp = await client.post(
        "/v1/sessions", headers=auth_headers, json=_payload(plan=plan)
    )
    assert resp.status_code == 201, resp.text
    features = resp.json()["plan_features"]
    assert features["session_max_minutes"] == expected_max
    assert features["stt_mode"] == expected_stt
    assert features["llm_prontuario_model"] == expected_model
    assert features["llm_realtime_red_flag"] is expected_realtime


@pytest.mark.asyncio
async def test_invalid_kind_returns_SESSION_INVALID_KIND(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = _payload(session_kind="not-a-kind")
    resp = await client.post("/v1/sessions", headers=auth_headers, json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "SESSION_INVALID_KIND"


@pytest.mark.asyncio
async def test_invalid_plan_returns_SESSION_INVALID_PLAN(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/v1/sessions", headers=auth_headers, json=_payload(plan="enterprise")
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "SESSION_INVALID_PLAN"


@pytest.mark.asyncio
async def test_invalid_seniority_returns_SESSION_INVALID_SENIORITY(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = _payload()
    payload["seniority"] = "principal"
    resp = await client.post("/v1/sessions", headers=auth_headers, json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "SESSION_INVALID_SENIORITY"


@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_400(client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer test", "Content-Type": "application/json"}
    resp = await client.post("/v1/sessions", headers=headers, json=_payload())
    assert resp.status_code == 400
    assert resp.json()["error"] == "IDEMPOTENCY_KEY_MISSING"


@pytest.mark.asyncio
async def test_missing_bearer_returns_401_AUTH_BEARER_MISSING(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/sessions",
        headers={"Idempotency-Key": "k", "Content-Type": "application/json"},
        json=_payload(),
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "AUTH_BEARER_MISSING"
    assert resp.headers.get("X-API-Version") == "1.0"


@pytest.mark.asyncio
async def test_auto_finalize_at_six_hours(
    client: AsyncClient, auth_headers: dict[str, str], db_engine: AsyncEngine
) -> None:
    resp = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    assert resp.status_code == 201
    session_id = uuid.UUID(resp.json()["session_id"])

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one()
        assert row.auto_finalize_at is not None
        assert row.created_at is not None
        delta = row.auto_finalize_at - row.created_at
        assert abs(delta - timedelta(hours=6)) < timedelta(seconds=2)


@pytest.mark.asyncio
async def test_close_inserts_draft_pending(
    client: AsyncClient, auth_headers: dict[str, str], db_engine: AsyncEngine
) -> None:
    create = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    sid = create.json()["session_id"]
    close = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert close.status_code == 200, close.text
    assert close.json()["draft_status"] == "pending"

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (
            await db.execute(select(Draft).where(Draft.session_id == uuid.UUID(sid)))
        ).scalar_one()
        assert row.draft_status == "pending"
        assert row.partial is False


@pytest.mark.asyncio
async def test_close_session_not_found_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        f"/v1/sessions/{uuid.uuid4()}/close", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_close_idempotent_already_closed_returns_409(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    sid = create.json()["session_id"]
    first = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert first.status_code == 200
    second = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert second.status_code == 409
    assert second.json()["error"] == "SESSION_ALREADY_CLOSED"


@pytest.mark.asyncio
async def test_token_refresh_returns_new_ws_token(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    sid = create.json()["session_id"]
    original = create.json()["ws_token"]

    refresh = await client.post(
        f"/v1/sessions/{sid}/token/refresh", headers=auth_headers
    )
    assert refresh.status_code == 200, refresh.text
    new_token = refresh.json()["ws_token"]
    assert new_token != original
    uuid.UUID(new_token)


@pytest.mark.asyncio
async def test_version_header_present_on_endpoints(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create = await client.post("/v1/sessions", headers=auth_headers, json=_payload())
    assert create.headers.get("X-API-Version") == "1.0"

    sid = create.json()["session_id"]
    close = await client.post(f"/v1/sessions/{sid}/close", headers=auth_headers)
    assert close.headers.get("X-API-Version") == "1.0"

    draft = await client.get(f"/v1/sessions/{sid}/draft", headers=auth_headers)
    assert draft.headers.get("X-API-Version") == "1.0"
