"""Issue #13 — Redis real atrás do protocolo ``EventStreamClient``.

AC1 — ``FakeEventStream.subscribe`` entrega só mensagens publicadas após a inscrição
AC2 — ``get_stream_client`` escolhe ``RedisEventStream`` quando ``REDIS_URL`` está definido
      e ``FakeEventStream`` quando vazio
AC3 — ``RedisEventStream`` cumpre o protocolo contra um Redis real
      (xadd/xrange/xrevrange/xlen/set+ex/get/expire/delete/publish/subscribe)
      — marker ``redis_real``; pula sem ``TEST_REDIS_URL``
AC4 — ``_watch_revocation`` fecha o WS com 4002 via ``subscribe`` (fake)
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from assistente_voz.server import stream_client as stream_client_module
from assistente_voz.server.config import reset_settings_cache
from assistente_voz.server.event_stream import (
    EventStreamClient,
    FakeEventStream,
    RedisEventStream,
)
from assistente_voz.server.redis_keys import (
    session_events_key,
    ws_revocations_channel,
    ws_token_key,
)

TENANT = "00000000-0000-0000-0000-000000000001"

requires_redis = pytest.mark.skipif(
    not os.getenv("TEST_REDIS_URL"),
    reason="TEST_REDIS_URL não definido (ex.: redis://localhost:6379/15)",
)


# ── AC1: subscribe semantics on the fake ─────────────────────────────────────

async def test_ac1_fake_subscribe_only_sees_messages_after_subscription():
    fake = FakeEventStream()
    channel = ws_revocations_channel(TENANT)
    await fake.publish(channel, "before")

    received: list[str] = []

    async def _consume() -> None:
        async for msg in fake.subscribe(channel):
            received.append(msg)
            if len(received) == 2:
                return

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)
    await fake.publish(channel, "one")
    await fake.publish(channel, "two")
    await asyncio.wait_for(task, timeout=2.0)

    assert received == ["one", "two"], "mensagens anteriores à inscrição não devem vazar"


# ── AC2: selection by REDIS_URL ──────────────────────────────────────────────

def test_ac2_stream_client_defaults_to_fake_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_settings_cache()
    stream_client_module.reset_stream_client()
    try:
        client = stream_client_module.get_stream_client()
        assert isinstance(client, FakeEventStream)
    finally:
        stream_client_module.reset_stream_client()
        reset_settings_cache()


def test_ac2_stream_client_uses_redis_when_url_set(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6399/0")
    reset_settings_cache()
    stream_client_module.reset_stream_client()
    try:
        client = stream_client_module.get_stream_client()
        # Lazy connection: constructing the adapter must not touch the network.
        assert isinstance(client, RedisEventStream)
        assert isinstance(client, EventStreamClient)
    finally:
        stream_client_module.reset_stream_client()
        reset_settings_cache()


def test_ac2_redis_url_credentials_are_redacted_in_logs():
    assert stream_client_module._redact("redis://user:secret@host:6379/0") == "redis://***@host:6379/0"
    assert stream_client_module._redact("redis://host:6379/0") == "redis://host:6379/0"


# ── AC3: real Redis conformance ──────────────────────────────────────────────

@pytest.mark.redis_real
@requires_redis
async def test_ac3_redis_event_stream_conforms_to_protocol():
    client = RedisEventStream(os.environ["TEST_REDIS_URL"])
    assert await client.ping()

    sid = str(uuid.uuid4())
    events_key = session_events_key(TENANT, sid)
    token_key = ws_token_key(TENANT, str(uuid.uuid4()))
    channel = ws_revocations_channel(TENANT) + f":{sid}"
    try:
        id1 = await client.xadd(events_key, {"seq": "0", "event_id": "a"})
        id2 = await client.xadd(events_key, {"seq": "1", "event_id": "b"})
        assert id1 and id2 and id1 < id2
        assert await client.xlen(events_key) == 2

        entries = await client.xrange(events_key)
        assert [f["seq"] for _, f in entries] == ["0", "1"]
        last = await client.xrevrange(events_key, count=1)
        assert last[0][1]["event_id"] == "b"

        await client.set(token_key, '{"x": 1}', ex=120)
        assert await client.get(token_key) == '{"x": 1}'
        await client.expire(events_key, 120)

        received: list[str] = []

        async def _consume() -> None:
            async for msg in client.subscribe(channel):
                received.append(msg)
                return

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.3)  # let SUBSCRIBE land
        await client.publish(channel, "revoke:x")
        await asyncio.wait_for(task, timeout=5.0)
        assert received == ["revoke:x"]

        await client.delete(token_key)
        assert await client.get(token_key) is None
    finally:
        await client.delete(events_key)
        await client.delete(token_key)
        await client.aclose()


# ── AC4: revocation watcher over subscribe ───────────────────────────────────

async def test_ac4_revocation_watcher_uses_subscribe():
    from assistente_voz.server.routers.audio import _watch_revocation

    fake = FakeEventStream()
    channel = ws_revocations_channel(TENANT)
    token = str(uuid.uuid4())
    closed: list[int] = []

    class _WS:
        async def close(self, code: int) -> None:
            closed.append(code)

    task = asyncio.create_task(_watch_revocation(fake, channel, token, _WS()))
    await asyncio.sleep(0.05)
    await fake.publish(channel, "revoke:someone-else")
    await fake.publish(channel, f"revoke:{token}")
    await asyncio.wait_for(task, timeout=2.0)
    assert closed == [4002]
