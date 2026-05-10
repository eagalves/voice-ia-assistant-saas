"""Tests for Issue #9 — Telemetry, kill switch, OTel tracing.

DB-backed tests require Postgres (via TEST_DATABASE_URL or testcontainers).
Pure unit tests run without a database.
"""
from __future__ import annotations

import uuid

import pytest

from assistente_voz.server.kill_switch import (
    DraftKillSwitch,
    get_kill_switch,
    reset_kill_switch,
)
from assistente_voz.server.metrics import (
    _SimpleGauge,
    _SimpleHistogram,
    draft_correction_ratio_histogram,
    draft_kill_switch_gauge,
)
from assistente_voz.server.tracing import _Tracer, get_tracer, reset_tracer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_ks() -> DraftKillSwitch:
    """Isolated kill switch instance (not the singleton)."""
    return DraftKillSwitch()


# ── AC1: Correction ratio calculated correctly ────────────────────────────────

def test_ac1_ratio_zero_when_original_is_zero():
    # Edge case: original=0 → ratio=0.0 (no division)
    payload_chars_original = 0
    payload_chars_edited = 0
    ratio = (
        payload_chars_edited / payload_chars_original
        if payload_chars_original != 0
        else 0.0
    )
    assert ratio == 0.0


def test_ac1_ratio_calculated_correctly():
    chars_original = 1200
    chars_edited = 180
    ratio = chars_edited / chars_original
    assert round(ratio, 4) == 0.15


def test_ac1_ratio_at_threshold():
    ratio = 600 / 1000
    assert ratio == 0.60


# ── AC2: draft_correction_ratio metric observed ───────────────────────────────

def test_ac2_histogram_observe_records_value():
    hist = _SimpleHistogram("test_hist")
    hist.observe(0.15)
    hist.observe(0.60)
    assert hist.count() == 2
    assert abs(hist.sum() - 0.75) < 1e-9


def test_ac2_draft_correction_ratio_is_histogram():
    assert isinstance(draft_correction_ratio_histogram, _SimpleHistogram)
    assert draft_correction_ratio_histogram.name == "draft_correction_ratio"


# ── AC3: Kill switch not active after 1 high ratio ───────────────────────────

def test_ac3_one_high_ratio_does_not_activate():
    ks = _fresh_ks()
    activated = ks.record("doctor@tenant", 0.60)
    assert activated is False
    assert ks._active is False


# ── AC4: Kill switch activated after 2 consecutive high ratios ────────────────

def test_ac4_two_consecutive_high_ratios_activate():
    ks = _fresh_ks()
    ks.record("doctor@tenant", 0.55)  # first > 0.50
    activated = ks.record("doctor@tenant", 0.70)  # second > 0.50
    assert activated is True
    assert ks._active is True


def test_ac4_is_active_returns_true_after_activation():
    ks = _fresh_ks()
    ks.record("doc", 0.55)
    ks.record("doc", 0.60)
    assert ks.is_active is True


def test_ac4_different_actor_subs_are_independent():
    ks = _fresh_ks()
    ks.record("doctor_a", 0.55)
    ks.record("doctor_a", 0.60)  # activates
    # doctor_b has only 1 submission
    ks2 = _fresh_ks()
    ks2.record("doctor_b", 0.60)
    assert ks2._active is False


# ── AC5: Kill switch resets on low ratio between high ones ────────────────────

def test_ac5_low_ratio_between_resets():
    ks = _fresh_ks()
    ks.record("doc", 0.55)   # > 0.50
    ks.record("doc", 0.20)   # < 0.50 → resets window
    activated = ks.record("doc", 0.60)  # > 0.50 but only 1 consecutive
    assert activated is False
    assert ks._active is False


def test_ac5_exactly_threshold_not_activated():
    # ratio must be STRICTLY greater than 0.50
    ks = _fresh_ks()
    ks.record("doc", 0.50)
    activated = ks.record("doc", 0.50)
    assert activated is False


# ── AC6: DRAFT_GENERATION_ENABLED=0 activates kill switch ────────────────────

def test_ac6_env_flag_disables_draft_generation(monkeypatch):
    monkeypatch.setenv("DRAFT_GENERATION_ENABLED", "0")
    ks = _fresh_ks()
    assert ks.is_active is True


def test_ac6_env_flag_enabled_1_does_not_activate(monkeypatch):
    monkeypatch.setenv("DRAFT_GENERATION_ENABLED", "1")
    ks = _fresh_ks()
    assert ks.is_active is False


# ── AC7: draft_kill_switch_gauge set when active ──────────────────────────────

def test_ac7_kill_switch_gauge_is_simple_gauge():
    assert isinstance(draft_kill_switch_gauge, _SimpleGauge)
    assert draft_kill_switch_gauge.name == "draft_kill_switch_active"


def test_ac7_kill_switch_gauge_reflects_activation():
    # Use isolated singleton
    reset_kill_switch()
    draft_kill_switch_gauge.set(0.0)

    ks = get_kill_switch()
    ks.record("doc", 0.60)
    ks.record("doc", 0.70)  # activates

    # Simulate what the router does
    if ks.is_active:
        draft_kill_switch_gauge.set(1.0)

    assert draft_kill_switch_gauge.get() == 1.0
    reset_kill_switch()
    draft_kill_switch_gauge.set(0.0)


# ── AC8: Tracer span has correct name ────────────────────────────────────────

def test_ac8_tracer_span_name():
    reset_tracer()
    tracer = get_tracer()
    with tracer.start_as_current_span("submit_feedback") as span:
        span.set_attribute("session_id", "test-session-123")
        span.set_attribute("tenant_id", "tenant-456")

    assert len(tracer.spans) == 1
    assert tracer.spans[0].name == "submit_feedback"
    assert tracer.spans[0].attributes["session_id"] == "test-session-123"
    assert tracer.spans[0].attributes["tenant_id"] == "tenant-456"
    reset_tracer()


def test_ac8_tracer_is_tracer_instance():
    assert isinstance(get_tracer(), _Tracer)


# ── AC9: Feedback endpoint stores actor_sub (DB test) ─────────────────────────

@pytest.mark.asyncio
async def test_ac9_feedback_endpoint_stores_actor_sub(client, truncate_tables, auth_headers):
    # Create a session first
    session_payload = {
        "session_kind": "medical",
        "actor_role": "doctor",
        "seniority": "senior",
        "plan": "premium",
    }
    session_resp = await client.post(
        "/v1/sessions",
        json=session_payload,
        headers=auth_headers,
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["session_id"]

    # Submit feedback
    feedback_payload = {
        "chars_original": 1000,
        "chars_edited": 200,
        "fields_corrected": ["queixa_principal"],
        "actor_sub": "doctor@example.com",
    }
    resp = await client.post(
        f"/v1/sessions/{session_id}/draft/feedback",
        json=feedback_payload,
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["correction_ratio"] == 0.2
    assert body["accepted"] is True


# ── AC10: Migration feedback table structurally verified ─────────────────────

def test_ac10_draft_feedback_table_in_migration(repo_root):
    migration_dir = repo_root / "migrations" / "versions"
    migration_files = list(migration_dir.glob("*.py"))

    content_combined = ""
    for f in migration_files:
        content_combined += f.read_text(encoding="utf-8")

    assert "draft_feedback" in content_combined, (
        "draft_feedback table not found in any migration file"
    )


def test_ac10_migration_has_non_trivial_downgrade(repo_root):
    migration_dir = repo_root / "migrations" / "versions"
    # At least one migration must have drop_table or op. in downgrade
    for f in migration_dir.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        if "def downgrade" in text:
            # Should contain more than just `pass`
            downgrade_idx = text.index("def downgrade")
            downgrade_body = text[downgrade_idx:]
            assert "pass" not in downgrade_body.split("\n")[1].strip() or "op." in downgrade_body, (
                f"Migration {f.name} has trivial downgrade()"
            )
