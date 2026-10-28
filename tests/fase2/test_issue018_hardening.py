"""Issue #18 — Hardening de contratos da Fase 2.0 (lote de bugs encontrados em revisão).

AC1  — ws_token de uma Sessão não abre o WS de outra Sessão (4001)
AC2  — WS sem ``X-Tenant-Id`` (browser) usa o tenant default e conecta
AC3  — Frame inválido → ``{"type":"error","reason":"FRAME_INVALID"}``; socket segue aberto
AC4  — Dois POST /v1/sessions concorrentes com a mesma Idempotency-Key → mesma Sessão
AC5  — POST /close aplica ``EXPIRE 86400`` no stream da Sessão
AC6  — AuditLog é alimentado pelos routers (created/closed/exported) e a cadeia verifica
AC7  — Gap na cadeia (linha removida com trigger desabilitada) é detectado (``reason=link``)
AC8  — UPDATE/DELETE direto em ``audit_log`` é bloqueado pela trigger append-only
AC9  — Refresh de token em Sessão fechada → 409; além do hard cap → 409 SESSION_HARD_CAP_REACHED
AC10 — Refresh não estende o token além do hard cap do plano
AC11 — POST /draft/retry reagenda a materialização (draft volta a ``ready``)
AC12 — JWKS: ``kid`` desconhecido após refresh → 401 (não 503); ``kid`` ausente → 401
AC13 — Feedback sem ``actor_sub`` usa o ``sub`` do JWT (bypass: bearer) e não um balde vazio
AC14 — ``correction_ratio`` > 10 é aceito (coluna FLOAT)
AC15 — Perfis interviewer exigem ``seniority`` → 400 SESSION_INVALID_SENIORITY
AC16 — ``APP_ENV=production`` sem ``JWT_JWKS_URL`` impede o boot (CONFIG_AUTH_BYPASS_FORBIDDEN)
AC17 — Resposta de criação informa ``ws_token_expires_at``
AC18 — WebSocket aceita com header ``x-session-id`` em formato ASGI (bytes)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from assistente_voz.server.audio_gateway import STREAM_TTL_SECONDS
from assistente_voz.server.audit_log import (
    EVENT_DRAFT_EXPORTED,
    EVENT_SESSION_CLOSED,
    EVENT_SESSION_CREATED,
    log_event,
    verify_chain,
)
from assistente_voz.server.auth.jwks_cache import JWKSCache, JWKSKeyNotFoundError
from assistente_voz.server.auth.jwt_validator import AuthError, JWTValidator
from assistente_voz.server.auth.jti_cache import JTICache
from assistente_voz.server.config import DEFAULT_TENANT_ID, reset_settings_cache
from assistente_voz.server.db.models import AuditLog, DraftFeedback, Session
from assistente_voz.server.event_stream import FakeEventStream
from assistente_voz.server.redis_keys import session_events_key
from assistente_voz.server.repositories.drafts_repo import DraftsRepository
from assistente_voz.server.stream_client import override_stream_client, reset_stream_client

_PAYLOAD = {"session_kind": "medical", "actor_role": "doctor", "seniority": None, "plan": "basico"}
_BEARER = {"Authorization": "Bearer test-token"}


def _headers() -> dict[str, str]:
    return {**_BEARER, "Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}


async def _create(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/v1/sessions", json=payload or _PAYLOAD, headers=_headers())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── AC1 / AC2 / AC3 / AC18 — WebSocket handshake ─────────────────────────────

@pytest.mark.usefixtures("truncate_tables")
async def test_ac1_token_bound_to_session(app):
    fake = FakeEventStream()
    override_stream_client(fake)
    try:
        with SyncTestClient(app) as tc:
            a = tc.post("/v1/sessions", json=_PAYLOAD, headers=_headers()).json()
            b = tc.post("/v1/sessions", json=_PAYLOAD, headers=_headers()).json()
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with tc.websocket_connect(
                    f"/v1/sessions/{b['session_id']}/audio?ws_token={a['ws_token']}&speaker=doctor"
                ):
                    pass
            assert exc_info.value.code == 4001
    finally:
        reset_stream_client()


@pytest.mark.usefixtures("truncate_tables")
async def test_ac2_ac18_ws_without_tenant_header_uses_default_tenant(app):
    fake = FakeEventStream()
    override_stream_client(fake)
    try:
        with SyncTestClient(app) as tc:
            created = tc.post("/v1/sessions", json=_PAYLOAD, headers=_headers()).json()
            with tc.websocket_connect(
                f"/v1/sessions/{created['session_id']}/audio?ws_token={created['ws_token']}"
            ) as ws:
                # ASGI accept headers must be bytes pairs (uvicorn rejects str).
                assert ws.extra_headers == [(b"x-session-id", created["session_id"].encode())]
                ws.send_text(json.dumps({"type": "ping"}))
                assert ws.receive_json() == {"type": "pong"}
    finally:
        reset_stream_client()


@pytest.mark.usefixtures("truncate_tables")
async def test_ac3_invalid_frame_reports_error_and_keeps_socket(app):
    fake = FakeEventStream()
    override_stream_client(fake)
    try:
        with SyncTestClient(app) as tc:
            created = tc.post("/v1/sessions", json=_PAYLOAD, headers=_headers()).json()
            with tc.websocket_connect(
                f"/v1/sessions/{created['session_id']}/audio?ws_token={created['ws_token']}"
            ) as ws:
                ws.send_text("not json")
                err = ws.receive_json()
                assert err["type"] == "error" and err["reason"] == "FRAME_INVALID"
                ws.send_text(json.dumps({"seq": "x", "data": "!!"}))
                err = ws.receive_json()
                assert err["reason"] == "FRAME_INVALID"
                ws.send_text(json.dumps({"type": "reconnect"}))
                assert ws.receive_json()["type"] == "session_resumed"
    finally:
        reset_stream_client()


# ── AC4 — idempotency race ───────────────────────────────────────────────────

async def test_ac4_concurrent_same_key_returns_same_session(client: AsyncClient, db_engine: AsyncEngine):
    headers = _headers()
    results = await asyncio.gather(
        client.post("/v1/sessions", json=_PAYLOAD, headers=headers),
        client.post("/v1/sessions", json=_PAYLOAD, headers=headers),
    )
    assert {r.status_code for r in results} == {201}
    ids = {r.json()["session_id"] for r in results}
    assert len(ids) == 1, "first-write-wins: ambos devem ver a mesma Sessão"
    assert sum(1 for r in results if r.headers.get("idempotency-replayed") == "true") == 1

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        rows = (await db.execute(select(Session))).scalars().all()
    assert len(rows) == 1, "a Sessão perdedora deve ser descartada (rollback)"


# ── AC5 / AC6 — close side effects + audit wiring ────────────────────────────

async def test_ac5_ac6_close_sets_stream_ttl_and_audit_chain(client: AsyncClient, db_engine: AsyncEngine):
    fake = FakeEventStream()
    override_stream_client(fake)
    try:
        created = await _create(client)
        sid = created["session_id"]
        resp = await client.post(f"/v1/sessions/{sid}/close", headers=_BEARER)
        assert resp.status_code == 200
        assert fake.ttl_for(session_events_key(DEFAULT_TENANT_ID, sid)) == STREAM_TTL_SECONDS

        sm = async_sessionmaker(db_engine, expire_on_commit=False)
        async with sm() as db:
            async with db.begin():
                draft = await DraftsRepository(db, DEFAULT_TENANT_ID).get_by_session(uuid.UUID(sid))
                assert draft is not None
                await DraftsRepository(db, DEFAULT_TENANT_ID).force_status(
                    draft, status="ready", content={"ok": True}
                )
        got = await client.get(f"/v1/sessions/{sid}/draft", headers=_BEARER)
        assert got.status_code == 200

        async with sm() as db:
            events = (
                await db.execute(
                    select(AuditLog).where(AuditLog.session_id == uuid.UUID(sid)).order_by(AuditLog.seq)
                )
            ).scalars().all()
            chain = await verify_chain(db, DEFAULT_TENANT_ID)
        types = [e.event_type for e in events]
        assert types[:2] == [EVENT_SESSION_CREATED, EVENT_SESSION_CLOSED]
        assert EVENT_DRAFT_EXPORTED in types
        assert all(e.actor_sub == "test-token" for e in events)
        assert chain["ok"] is True and chain["total"] >= 3
    finally:
        reset_stream_client()


# ── AC7 / AC8 — audit log integrity ──────────────────────────────────────────

async def test_ac7_gap_in_chain_is_detected(db_engine: AsyncEngine):
    tenant_id = uuid.uuid4()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        for i in range(3):
            await log_event(db, tenant_id=tenant_id, event_type="t", payload={"i": i})
        await db.commit()
        middle = (
            await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq))
        ).scalars().all()[1]
        await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER trg_audit_log_append_only"))
        await db.execute(text("DELETE FROM audit_log WHERE id = :id AND tenant_id = :tid"), {"id": middle.id, "tid": tenant_id})
        await db.execute(text("ALTER TABLE audit_log ENABLE TRIGGER trg_audit_log_append_only"))
        await db.commit()
        chain = await verify_chain(db, tenant_id)
    assert chain["ok"] is False
    assert chain["total"] == 2
    assert [b["reason"] for b in chain["broken"]] == ["link"]


async def test_ac7_two_events_in_one_transaction_keep_chain_order(db_engine: AsyncEngine):
    tenant_id = uuid.uuid4()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            first = await log_event(db, tenant_id=tenant_id, event_type="a", payload={})
            second = await log_event(db, tenant_id=tenant_id, event_type="b", payload={})
        assert second.prev_hash == first.event_hash
        assert second.seq > first.seq
        chain = await verify_chain(db, tenant_id)
    assert chain["ok"] is True and chain["total"] == 2


async def test_ac8_update_and_delete_blocked_by_trigger(db_engine: AsyncEngine):
    tenant_id = uuid.uuid4()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        await log_event(db, tenant_id=tenant_id, event_type="t", payload={"x": 1})
        await db.commit()
    for stmt in (
        "UPDATE audit_log SET actor_sub = 'x' WHERE tenant_id = :tid",
        "DELETE FROM audit_log WHERE tenant_id = :tid",
    ):
        async with db_engine.connect() as conn:
            with pytest.raises(Exception) as exc_info:
                await conn.execute(text(stmt), {"tid": tenant_id})
            assert "append-only" in str(exc_info.value)


# ── AC9 / AC10 — token refresh limits ────────────────────────────────────────

async def test_ac9_refresh_rejected_on_closed_session(client: AsyncClient):
    created = await _create(client)
    sid = created["session_id"]
    assert (await client.post(f"/v1/sessions/{sid}/close", headers=_BEARER)).status_code == 200
    resp = await client.post(f"/v1/sessions/{sid}/token/refresh", headers=_BEARER)
    assert resp.status_code == 409
    assert resp.json()["error"] == "SESSION_ALREADY_CLOSED"


async def test_ac9_ac10_refresh_capped_by_plan_hard_cap(client: AsyncClient, db_engine: AsyncEngine):
    fake = FakeEventStream()
    override_stream_client(fake)
    try:
        created = await _create(client)  # basico → 60 min hard cap
        sid = uuid.UUID(created["session_id"])
        sm = async_sessionmaker(db_engine, expire_on_commit=False)

        # 50 min in: refresh allowed but TTL ≤ 10 min (not a fresh 60 min).
        async with sm() as db:
            async with db.begin():
                row = (await db.execute(select(Session).where(Session.id == sid))).scalar_one()
                row.created_at = datetime.now(timezone.utc) - timedelta(minutes=50)
        resp = await client.post(f"/v1/sessions/{sid}/token/refresh", headers=_BEARER)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ttl = fake.ttl_for(f"ws:token:{{{DEFAULT_TENANT_ID}}}:{body['ws_token']}")
        assert ttl is not None and 0 < ttl <= 10 * 60 + 5
        expires = datetime.fromisoformat(body["ws_token_expires_at"].replace("Z", "+00:00"))
        assert expires - datetime.now(timezone.utc) < timedelta(minutes=11)

        # Past the hard cap: refused.
        async with sm() as db:
            async with db.begin():
                row = (await db.execute(select(Session).where(Session.id == sid))).scalar_one()
                row.created_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        resp = await client.post(f"/v1/sessions/{sid}/token/refresh", headers=_BEARER)
        assert resp.status_code == 409
        assert resp.json()["error"] == "SESSION_HARD_CAP_REACHED"
    finally:
        reset_stream_client()


# ── AC11 — retry schedules materialization ───────────────────────────────────

async def test_ac11_retry_reschedules_materialization(client: AsyncClient, db_engine: AsyncEngine):
    created = await _create(client)
    sid = created["session_id"]
    assert (await client.post(f"/v1/sessions/{sid}/close", headers=_BEARER)).status_code == 200
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        async with db.begin():
            repo = DraftsRepository(db, DEFAULT_TENANT_ID)
            draft = await repo.get_by_session(uuid.UUID(sid))
            await repo.force_status(draft, status="failed", last_error="boom")
    resp = await client.post(f"/v1/sessions/{sid}/draft/retry", headers=_BEARER)
    assert resp.status_code == 202
    got = await client.get(f"/v1/sessions/{sid}/draft", headers=_BEARER)
    assert got.status_code == 200, "retry deve reagendar o worker e deixar o draft ready"
    assert got.json()["content"]["event_count"] >= 0


# ── AC12 — JWKS ──────────────────────────────────────────────────────────────

def _rsa_keypair(kid: str = "k1"):
    import base64

    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()

    def _b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64(numbers.n), "e": _b64(numbers.e)}
    return private_key, jwk


def _rs256_token(private_key, kid: str | None) -> str:
    import jwt as pyjwt

    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com", "sub": "user-1", "aud": "urn:assistente-voz:fase2",
        "iat": now, "exp": now + 300, "nbf": now, "jti": str(uuid.uuid4()),
    }
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(claims, private_key, algorithm="RS256", headers=headers)


async def test_ac12_unknown_kid_after_refresh_is_401_not_503():
    priv, jwk = _rsa_keypair("k1")

    async def _fetcher(url: str) -> dict:
        return {"keys": [jwk]}

    jwks = JWKSCache("https://idp/.well-known/jwks.json", _fetcher)
    validator = JWTValidator(jwks_cache=jwks, jti_cache=JTICache(), iss_allowlist=[])

    with pytest.raises(JWKSKeyNotFoundError):
        await jwks.get_key("not-a-key")

    with pytest.raises(AuthError) as exc_info:
        await validator.validate(_rs256_token(priv, "rotated-kid"))
    assert exc_info.value.code == "AUTH_JWT_INVALID"


async def test_ac12_missing_kid_is_401():
    priv, jwk = _rsa_keypair()
    calls: list[str] = []

    async def _fetcher(url: str) -> dict:
        calls.append(url)
        return {"keys": [jwk]}

    validator = JWTValidator(
        jwks_cache=JWKSCache("https://idp/jwks", _fetcher), jti_cache=JTICache(), iss_allowlist=[]
    )
    with pytest.raises(AuthError) as exc_info:
        await validator.validate(_rs256_token(priv, None))
    assert exc_info.value.code == "AUTH_JWT_INVALID"
    assert calls == [], "sem kid não há refresh de JWKS (sem amplificação contra o IdP)"


async def test_ac12_refresh_on_miss_is_rate_limited():
    tick = [100.0]
    calls: list[str] = []

    async def _fetcher(url: str) -> dict:
        calls.append(url)
        return {"keys": []}

    jwks = JWKSCache("https://idp/jwks", _fetcher, clock=lambda: tick[0], min_refresh_interval_s=5.0)
    with pytest.raises(JWKSKeyNotFoundError):
        await jwks.get_key("a")
    with pytest.raises(JWKSKeyNotFoundError):
        await jwks.get_key("b")
    assert len(calls) == 1, "segunda falta dentro de 5s não refaz o fetch"
    tick[0] += 6.0
    with pytest.raises(JWKSKeyNotFoundError):
        await jwks.get_key("c")
    assert len(calls) == 2


# ── AC13 / AC14 — feedback ───────────────────────────────────────────────────

async def test_ac13_feedback_actor_sub_falls_back_to_jwt_sub(client: AsyncClient, db_engine: AsyncEngine):
    created = await _create(client)
    sid = created["session_id"]
    resp = await client.post(
        f"/v1/sessions/{sid}/draft/feedback",
        json={"chars_original": 100, "chars_edited": 10, "fields_corrected": []},
        headers={**_BEARER, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db:
        row = (
            await db.execute(select(DraftFeedback).where(DraftFeedback.session_id == uuid.UUID(sid)))
        ).scalar_one()
    assert row.actor_sub == "test-token"


async def test_ac14_feedback_ratio_above_ten_is_accepted(client: AsyncClient):
    created = await _create(client)
    sid = created["session_id"]
    resp = await client.post(
        f"/v1/sessions/{sid}/draft/feedback",
        json={"chars_original": 1, "chars_edited": 500, "fields_corrected": []},
        headers={**_BEARER, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["correction_ratio"] == 500.0


# ── AC15 / AC17 — create contract ────────────────────────────────────────────

async def test_ac15_interviewer_requires_seniority(client: AsyncClient):
    payload = {**_PAYLOAD, "session_kind": "general_interviewer", "seniority": None}
    resp = await client.post("/v1/sessions", json=payload, headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error"] == "SESSION_INVALID_SENIORITY"
    ok = await client.post("/v1/sessions", json={**payload, "seniority": "pleno"}, headers=_headers())
    assert ok.status_code == 201


async def test_ac17_create_returns_token_expiry(client: AsyncClient):
    created = await _create(client)
    expires = datetime.fromisoformat(created["ws_token_expires_at"].replace("Z", "+00:00"))
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=58) < delta <= timedelta(minutes=60)


# ── AC16 — production guard ──────────────────────────────────────────────────

def test_ac16_production_without_jwks_refuses_to_boot(monkeypatch):
    from assistente_voz.server.app import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("JWT_JWKS_URL", raising=False)
    reset_settings_cache()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            create_app()
        assert "CONFIG_AUTH_BYPASS_FORBIDDEN" in str(exc_info.value)
    finally:
        reset_settings_cache()
