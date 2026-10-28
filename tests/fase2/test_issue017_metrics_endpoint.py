"""Issue #17 — ``GET /metrics`` com ``prometheus_client`` (label ``tenant_id`` em tudo).

AC1 — ``/metrics`` responde 200 sem Bearer (fora de ``/v1``) no formato de exposição Prometheus
AC2 — Toda métrica exportada carrega o label ``tenant_id``
AC3 — Stubs mantêm a API usada pelos testes (``set/get``, ``observe/sum/count``) e espelham
       no registry real quando ``prometheus_client`` está instalado
AC4 — ``/health`` informa o backend de EventStream e de STT ativos
"""
from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from assistente_voz.server import metrics as m


async def test_ac1_metrics_endpoint_is_public_and_prometheus_formatted(client: AsyncClient):
    m.stt_circuit_state_gauge.set(0.0)
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "stt_circuit_state" in resp.text
    assert "draft_kill_switch_active" in resp.text


async def test_ac2_every_sample_carries_tenant_id_label(client: AsyncClient):
    m.draft_correction_ratio_histogram.observe(0.1)
    m.stt_fallback_total.inc(reason="timeout")
    resp = await client.get("/metrics")
    samples = [
        line for line in resp.text.splitlines()
        if line and not line.startswith("#") and "{" in line
    ]
    assert samples, "esperado ao menos uma amostra com labels"
    for line in samples:
        assert re.search(r'tenant_id="[0-9a-f-]{36}"', line), f"sem tenant_id: {line}"


def test_ac3_stubs_mirror_into_registry():
    gauge = m._SimpleGauge("test_issue017_gauge", "doc")
    gauge.set(3.0, tenant_id="11111111-1111-1111-1111-111111111111")
    assert gauge.get() == 3.0
    hist = m._SimpleHistogram("test_issue017_hist", "doc")
    hist.observe(0.25)
    hist.observe(0.75)
    assert hist.count() == 2 and hist.sum() == pytest.approx(1.0)
    counter = m._SimpleCounter("test_issue017_counter", "doc", extra_labels=("reason",))
    counter.inc(reason="x")
    counter.inc(2, reason="x")
    assert counter.value(reason="x") == 3.0
    body, _ = m.render_metrics()
    text = body.decode()
    assert 'test_issue017_gauge{tenant_id="11111111-1111-1111-1111-111111111111"} 3.0' in text
    assert "test_issue017_counter_total" in text


def test_ac3_labels_without_tenant_rejected():
    gauge = m._SimpleGauge("test_issue017_gauge_two", "doc")
    with pytest.raises(ValueError):
        gauge.set(1.0, tenant_id="")


async def test_ac4_health_reports_backends(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["event_stream"] in {"memory", "redis"}
    assert body["stt_backend"] in {"off", "fake", "local_whisper"}
