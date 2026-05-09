"""Tests for Issue #8 — Draft field confidence scoring with feature flag."""
from __future__ import annotations

import pytest

from assistente_voz.server.field_confidence import (
    FieldConfidenceConfig,
    classify_field_confidence,
    enrich_draft_content,
    get_field_confidence_config,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_content(fields: dict | None = None) -> dict:
    """Build a minimal draft content dict with optional fields."""
    return {
        "events": [],
        "event_count": 0,
        "materialized_at": "2026-05-09T00:00:00+00:00",
        **({"fields": fields} if fields is not None else {}),
    }


def _enabled_cfg(review: float = 0.55, high: float = 0.80) -> FieldConfidenceConfig:
    return FieldConfidenceConfig(enabled=True, review_threshold=review, high_threshold=high)


def _disabled_cfg() -> FieldConfidenceConfig:
    return FieldConfidenceConfig(enabled=False, review_threshold=0.55, high_threshold=0.80)


# ── AC1: Disabled — no confidence fields ─────────────────────────────────────

def test_ac1_disabled_no_confidence_class():
    content = _make_content(
        fields={
            "queixa_principal": {"value": "dor", "field_confidence": 0.42},
        }
    )
    result = enrich_draft_content(content, _disabled_cfg())
    field = result["fields"]["queixa_principal"]
    assert "confidence_class" not in field


def test_ac1_disabled_returns_content_unchanged():
    content = _make_content(
        fields={"f": {"value": "v", "field_confidence": 0.9}}
    )
    result = enrich_draft_content(content, _disabled_cfg())
    # When disabled, content is returned without modification
    assert result["fields"]["f"] == content["fields"]["f"]
    assert "confidence_class" not in result["fields"]["f"]


# ── AC2: Enabled — all fields get confidence_class ───────────────────────────

def test_ac2_enabled_all_fields_have_confidence_class():
    content = _make_content(
        fields={
            "campo_a": {"value": "x", "field_confidence": 0.42},
            "campo_b": {"value": "y", "field_confidence": 0.67},
            "campo_c": {"value": "z", "field_confidence": 0.85},
        }
    )
    result = enrich_draft_content(content, _enabled_cfg())
    for field_name, field_data in result["fields"].items():
        assert "confidence_class" in field_data, f"{field_name} missing confidence_class"


def test_ac2_enabled_no_fields_key_returns_unchanged():
    content = _make_content()  # no "fields" key
    result = enrich_draft_content(content, _enabled_cfg())
    assert "fields" not in result


# ── AC3: field_confidence=0.42 → revisar ─────────────────────────────────────

def test_ac3_field_confidence_042_is_revisar():
    cfg = _enabled_cfg(review=0.55, high=0.80)
    assert classify_field_confidence(0.42, cfg) == "revisar"


def test_ac3_field_confidence_below_review_threshold():
    content = _make_content(fields={"f": {"value": "v", "field_confidence": 0.42}})
    result = enrich_draft_content(content, _enabled_cfg())
    assert result["fields"]["f"]["confidence_class"] == "revisar"


# ── AC4: field_confidence=0.67 → medio ───────────────────────────────────────

def test_ac4_field_confidence_067_is_medio():
    cfg = _enabled_cfg(review=0.55, high=0.80)
    assert classify_field_confidence(0.67, cfg) == "medio"


def test_ac4_field_confidence_between_thresholds():
    content = _make_content(fields={"f": {"value": "v", "field_confidence": 0.67}})
    result = enrich_draft_content(content, _enabled_cfg())
    assert result["fields"]["f"]["confidence_class"] == "medio"


# ── AC5: field_confidence=0.85 → alto ────────────────────────────────────────

def test_ac5_field_confidence_085_is_alto():
    cfg = _enabled_cfg(review=0.55, high=0.80)
    assert classify_field_confidence(0.85, cfg) == "alto"


def test_ac5_field_confidence_at_or_above_high():
    content = _make_content(fields={"f": {"value": "v", "field_confidence": 0.85}})
    result = enrich_draft_content(content, _enabled_cfg())
    assert result["fields"]["f"]["confidence_class"] == "alto"


# ── AC6: Custom review threshold respected ───────────────────────────────────

def test_ac6_custom_review_threshold_env(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "0.70")
    monkeypatch.setenv("FIELD_CONFIDENCE_HIGH_THRESHOLD", "0.90")

    cfg = get_field_confidence_config()
    assert cfg.enabled is True
    assert cfg.review_threshold == 0.70
    # A score of 0.65 should be "revisar" with threshold=0.70
    assert classify_field_confidence(0.65, cfg) == "revisar"


def test_ac6_custom_threshold_direct():
    cfg = _enabled_cfg(review=0.70, high=0.90)
    assert classify_field_confidence(0.65, cfg) == "revisar"
    assert classify_field_confidence(0.75, cfg) == "medio"
    assert classify_field_confidence(0.92, cfg) == "alto"


# ── AC7: Inverted thresholds fall back to defaults ───────────────────────────

def test_ac7_inverted_thresholds_use_defaults(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "0.80")
    monkeypatch.setenv("FIELD_CONFIDENCE_HIGH_THRESHOLD", "0.55")

    cfg = get_field_confidence_config()
    # Should fall back to defaults: review=0.55, high=0.80
    assert cfg.review_threshold == 0.55
    assert cfg.high_threshold == 0.80


def test_ac7_equal_thresholds_use_defaults(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "0.70")
    monkeypatch.setenv("FIELD_CONFIDENCE_HIGH_THRESHOLD", "0.70")

    cfg = get_field_confidence_config()
    assert cfg.review_threshold == 0.55
    assert cfg.high_threshold == 0.80


# ── AC8: Out-of-range threshold uses defaults ─────────────────────────────────

def test_ac8_out_of_range_below_minimum(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "0.10")  # below 0.30
    monkeypatch.setenv("FIELD_CONFIDENCE_HIGH_THRESHOLD", "0.80")

    cfg = get_field_confidence_config()
    assert cfg.review_threshold == 0.55
    assert cfg.high_threshold == 0.80


def test_ac8_out_of_range_above_maximum(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "0.55")
    monkeypatch.setenv("FIELD_CONFIDENCE_HIGH_THRESHOLD", "0.99")  # above 0.95

    cfg = get_field_confidence_config()
    assert cfg.review_threshold == 0.55
    assert cfg.high_threshold == 0.80


def test_ac8_non_numeric_threshold_uses_defaults(monkeypatch):
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    monkeypatch.setenv("FIELD_CONFIDENCE_REVIEW_THRESHOLD", "not_a_number")

    cfg = get_field_confidence_config()
    assert cfg.review_threshold == 0.55
    assert cfg.high_threshold == 0.80


# ── AC9: Feature flag re-read per materialization ─────────────────────────────

def test_ac9_flag_reread_between_calls(monkeypatch):
    # First call: disabled
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "0")
    cfg1 = get_field_confidence_config()
    assert cfg1.enabled is False

    # Change env without restart
    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    cfg2 = get_field_confidence_config()
    assert cfg2.enabled is True


def test_ac9_enrich_uses_fresh_config_each_time(monkeypatch):
    content = _make_content(fields={"f": {"value": "v", "field_confidence": 0.42}})

    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "0")
    result1 = enrich_draft_content(content, get_field_confidence_config())
    assert "confidence_class" not in result1["fields"]["f"]

    monkeypatch.setenv("DRAFT_FIELD_CONFIDENCE_ENABLED", "1")
    result2 = enrich_draft_content(content, get_field_confidence_config())
    assert "confidence_class" in result2["fields"]["f"]
