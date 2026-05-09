"""Tests for Issue #7 — CircuitBreakerConfig + stt_circuit_state metric.

All tests use fake backends and injectable clock — no network calls, no asyncio mark.
"""
from __future__ import annotations

import os

import pytest

from assistente_voz.server.stt_service import (
    CircuitBreakerConfig,
    FakeBatchBackend,
    FakeStreamingBackend,
    STTCircuitBreaker,
    STTStreamService,
)
from assistente_voz.server.metrics import stt_circuit_state_gauge, _SimpleGauge


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_clock(start: float = 0.0):
    """Returns a mutable clock callable backed by a list[float]."""
    t = [start]

    def clock() -> float:
        return t[0]

    def advance(seconds: float) -> None:
        t[0] += seconds

    clock.advance = advance  # type: ignore[attr-defined]
    return clock


def _noop_final(_event):
    pass


# ── AC1: 3 consecutive timeouts open the circuit ──────────────────────────────

def test_ac1_3_timeouts_open_circuit():
    clock = _make_clock()
    cb = STTCircuitBreaker(clock=clock)
    assert not cb.is_open

    for _ in range(3):
        opened = cb.record_failure()
    assert opened is True
    assert cb.is_open


def test_ac1_less_than_3_failures_keep_circuit_closed():
    cb = STTCircuitBreaker()
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open


# ── AC2: Open circuit routes to batch backend ─────────────────────────────────

@pytest.mark.asyncio
async def test_ac2_open_circuit_uses_batch_backend():
    clock = _make_clock()
    cb = STTCircuitBreaker(clock=clock)
    # Open the circuit
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open

    batch = FakeBatchBackend()
    streaming = FakeStreamingBackend()  # should NOT be called
    service = STTStreamService(streaming, batch, _circuit_breaker=cb)

    await service.process_chunk(b"audio", on_final=_noop_final)
    assert batch.call_count == 1
    assert streaming.stream_call_count == 0


# ── AC3: Probe not started before min window ──────────────────────────────────

@pytest.mark.asyncio
async def test_ac3_probe_not_started_before_min_window():
    clock = _make_clock(start=0.0)
    cb = STTCircuitBreaker(clock=clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open

    # Advance only 30s — still within min window
    clock.advance(30.0)
    closed = cb.record_health_ok()
    assert closed is False
    assert cb.is_open


# ── AC4: 5 probes OK after ≥60s close the circuit ────────────────────────────

@pytest.mark.asyncio
async def test_ac4_5_probes_close_circuit():
    clock = _make_clock(start=0.0)
    ws_events: list[dict] = []
    cb = STTCircuitBreaker(clock=clock)
    batch = FakeBatchBackend()
    streaming = FakeStreamingBackend()
    streaming.health_ok = True
    service = STTStreamService(
        streaming,
        batch,
        _circuit_breaker=cb,
        on_ws_status=ws_events.append,
    )

    # Open the circuit
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open

    # Advance past min window
    clock.advance(65.0)

    # 4 probes → still open
    for _ in range(4):
        closed = cb.record_health_ok()
        assert not closed

    # 5th probe → circuit closes
    closed = cb.record_health_ok()
    assert closed is True
    assert not cb.is_open

    # status event emitted via run_health_probe (already closed, no-op)
    await service.run_health_probe()


@pytest.mark.asyncio
async def test_ac4_close_event_has_reason_circuit_closed():
    clock = _make_clock(start=0.0)
    ws_events: list[dict] = []
    cb = STTCircuitBreaker(clock=clock)
    streaming = FakeStreamingBackend()
    streaming.health_ok = True
    service = STTStreamService(
        streaming,
        FakeBatchBackend(),
        _circuit_breaker=cb,
        on_ws_status=ws_events.append,
    )

    for _ in range(3):
        cb.record_failure()
    clock.advance(65.0)
    for _ in range(5):
        await service.run_health_probe()

    assert any(
        e.get("stt_mode") == "streaming" and e.get("reason") == "circuit_closed"
        for e in ws_events
    ), f"Expected circuit_closed event, got: {ws_events}"


# ── AC5: Probe failure resets count ──────────────────────────────────────────

def test_ac5_probe_fail_resets_health_count():
    clock = _make_clock(start=0.0)
    cb = STTCircuitBreaker(clock=clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(65.0)

    # 4 probes OK
    for _ in range(4):
        cb.record_health_ok()

    # 1 fail — resets count
    cb.record_health_fail()

    # Need 5 more probes now
    for i in range(4):
        closed = cb.record_health_ok()
        assert not closed, f"Should still be open after {i+1} probes post-reset"

    closed = cb.record_health_ok()
    assert closed is True


# ── AC6: Session stays alive during fallback ──────────────────────────────────

@pytest.mark.asyncio
async def test_ac6_session_stays_alive_during_fallback():
    clock = _make_clock()
    cb = STTCircuitBreaker(clock=clock)
    for _ in range(3):
        cb.record_failure()

    batch = FakeBatchBackend()
    service = STTStreamService(
        FakeStreamingBackend(),
        batch,
        _circuit_breaker=cb,
    )

    # Process 5 chunks — should not raise
    for _ in range(5):
        await service.process_chunk(b"audio", on_final=_noop_final)

    assert batch.call_count == 5


# ── AC7: stt_circuit_state metric ────────────────────────────────────────────

def test_ac7_stt_circuit_state_metric_open():
    # Reset the gauge to known state
    stt_circuit_state_gauge.set(0.0)

    clock = _make_clock()
    cb = STTCircuitBreaker(clock=clock)
    for _ in range(3):
        cb.record_failure()

    assert cb.is_open
    assert stt_circuit_state_gauge.get() == 1.0


def test_ac7_stt_circuit_state_metric_closed():
    stt_circuit_state_gauge.set(0.0)

    clock = _make_clock(start=0.0)
    cb = STTCircuitBreaker(clock=clock)
    for _ in range(3):
        cb.record_failure()
    assert stt_circuit_state_gauge.get() == 1.0

    clock.advance(65.0)
    for _ in range(5):
        cb.record_health_ok()

    assert not cb.is_open
    assert stt_circuit_state_gauge.get() == 0.0


def test_ac7_stt_circuit_state_gauge_is_simple_gauge():
    assert isinstance(stt_circuit_state_gauge, _SimpleGauge)
    assert stt_circuit_state_gauge.name == "stt_circuit_state"


# ── AC8: CircuitBreakerConfig from env ────────────────────────────────────────

def test_ac8_config_defaults():
    cfg = CircuitBreakerConfig()
    assert cfg.failure_threshold == 3
    assert cfg.fallback_min_window_s == 60
    assert cfg.health_check_success_required == 5
    assert cfg.probe_interval_s == 10
    assert cfg.timeout_per_segment_ms == 800
    assert cfg.timeout_s == 0.8


def test_ac8_config_from_env_reads_values(monkeypatch):
    monkeypatch.setenv("CB_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("CB_FALLBACK_MIN_WINDOW_S", "120")
    monkeypatch.setenv("CB_HEALTH_CHECK_SUCCESS_REQUIRED", "3")
    monkeypatch.setenv("CB_PROBE_INTERVAL_S", "15")
    monkeypatch.setenv("CB_TIMEOUT_PER_SEGMENT_MS", "1200")

    cfg = CircuitBreakerConfig.from_env()
    assert cfg.failure_threshold == 5
    assert cfg.fallback_min_window_s == 120
    assert cfg.health_check_success_required == 3
    assert cfg.probe_interval_s == 15
    assert cfg.timeout_per_segment_ms == 1200
    assert cfg.timeout_s == 1.2


def test_ac8_config_from_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CB_FAILURE_THRESHOLD", "not_an_int")
    cfg = CircuitBreakerConfig.from_env()
    assert cfg.failure_threshold == 3  # default


def test_ac8_cb_uses_config_threshold():
    cfg = CircuitBreakerConfig(failure_threshold=2)
    cb = STTCircuitBreaker(config=cfg)

    cb.record_failure()
    assert not cb.is_open

    opened = cb.record_failure()
    assert opened is True
    assert cb.is_open


@pytest.mark.asyncio
async def test_ac8_open_event_has_reason_circuit_open():
    ws_events: list[dict] = []
    cfg = CircuitBreakerConfig(failure_threshold=1)
    cb = STTCircuitBreaker(config=cfg)
    streaming = FakeStreamingBackend(timeout=True)
    service = STTStreamService(
        streaming,
        FakeBatchBackend(),
        _circuit_breaker=cb,
        on_ws_status=ws_events.append,
        _timeout_s=0.001,
    )

    await service.process_chunk(b"audio", on_final=_noop_final)
    assert any(
        e.get("stt_mode") == "fallback_batch" and e.get("reason") == "circuit_open"
        for e in ws_events
    ), f"Expected circuit_open event, got: {ws_events}"
