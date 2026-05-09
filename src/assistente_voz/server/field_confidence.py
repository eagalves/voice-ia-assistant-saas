"""Draft field confidence scoring — Issue #8.

Classifies each draft field with a confidence level based on the STT
``confidence`` scores of the segments that contributed to that field.

Feature flag
------------
Set ``DRAFT_FIELD_CONFIDENCE_ENABLED=1`` to activate.  When disabled
(default) the draft schema is identical to the pre-flag version.

Env vars
--------
DRAFT_FIELD_CONFIDENCE_ENABLED=0        # 0=off (default), 1=on
FIELD_CONFIDENCE_REVIEW_THRESHOLD=0.55  # below → "revisar"
FIELD_CONFIDENCE_HIGH_THRESHOLD=0.80    # at or above → "alto"

Validation: ``REVIEW_THRESHOLD < HIGH_THRESHOLD``; values outside
``[0.30, 0.95]`` → warning + fallback to defaults.  Re-read on every call
so a flag change takes effect without restarting the server.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

CONFIDENCE_ENABLED_ENV = "DRAFT_FIELD_CONFIDENCE_ENABLED"
REVIEW_THRESHOLD_ENV = "FIELD_CONFIDENCE_REVIEW_THRESHOLD"
HIGH_THRESHOLD_ENV = "FIELD_CONFIDENCE_HIGH_THRESHOLD"

_DEFAULT_REVIEW: float = 0.55
_DEFAULT_HIGH: float = 0.80
_RANGE_MIN: float = 0.30
_RANGE_MAX: float = 0.95


@dataclass
class FieldConfidenceConfig:
    """Runtime parameters for field confidence classification."""

    enabled: bool
    review_threshold: float
    high_threshold: float


def get_field_confidence_config() -> FieldConfidenceConfig:
    """Read env vars and return a validated config.

    Called at the start of each draft materialization so changes to env
    take effect without a restart.
    """
    enabled = os.getenv(CONFIDENCE_ENABLED_ENV, "0") == "1"

    raw_review = os.getenv(REVIEW_THRESHOLD_ENV, str(_DEFAULT_REVIEW))
    raw_high = os.getenv(HIGH_THRESHOLD_ENV, str(_DEFAULT_HIGH))

    try:
        review = float(raw_review)
        high = float(raw_high)
    except ValueError:
        _logger.warning(
            "field_confidence: could not parse threshold env vars "
            "(REVIEW=%r, HIGH=%r); using defaults %.2f/%.2f",
            raw_review,
            raw_high,
            _DEFAULT_REVIEW,
            _DEFAULT_HIGH,
        )
        return FieldConfidenceConfig(
            enabled=enabled,
            review_threshold=_DEFAULT_REVIEW,
            high_threshold=_DEFAULT_HIGH,
        )

    # Range check
    if not (_RANGE_MIN <= review <= _RANGE_MAX) or not (_RANGE_MIN <= high <= _RANGE_MAX):
        _logger.warning(
            "field_confidence: threshold(s) outside [%.2f, %.2f] "
            "(review=%.2f, high=%.2f); using defaults",
            _RANGE_MIN,
            _RANGE_MAX,
            review,
            high,
        )
        return FieldConfidenceConfig(
            enabled=enabled,
            review_threshold=_DEFAULT_REVIEW,
            high_threshold=_DEFAULT_HIGH,
        )

    # Ordering check
    if review >= high:
        _logger.warning(
            "field_confidence: REVIEW_THRESHOLD (%.2f) >= HIGH_THRESHOLD (%.2f); "
            "using defaults",
            review,
            high,
        )
        return FieldConfidenceConfig(
            enabled=enabled,
            review_threshold=_DEFAULT_REVIEW,
            high_threshold=_DEFAULT_HIGH,
        )

    return FieldConfidenceConfig(
        enabled=enabled,
        review_threshold=review,
        high_threshold=high,
    )


def classify_field_confidence(
    field_confidence: float,
    config: FieldConfidenceConfig,
) -> str:
    """Map a field confidence score to a human-readable classification.

    Returns one of: ``"revisar"``, ``"medio"``, ``"alto"``.
    """
    if field_confidence < config.review_threshold:
        return "revisar"
    if field_confidence < config.high_threshold:
        return "medio"
    return "alto"


def enrich_draft_content(
    content: dict,
    config: FieldConfidenceConfig,
) -> dict:
    """Add ``confidence_class`` to each field in ``content["fields"]``.

    When ``config.enabled`` is False returns *content* unchanged.

    Expects ``content["fields"]`` to be a ``dict[str, dict]`` where each
    inner dict may contain ``field_confidence`` (float) and
    ``uncertain_segments`` (list[str]).  Fields without ``field_confidence``
    default to 1.0 (treated as high confidence).
    """
    if not config.enabled:
        return content

    raw_fields = content.get("fields")
    if not isinstance(raw_fields, dict):
        return content

    enriched_fields: dict = {}
    for field_name, field_data in raw_fields.items():
        fc = field_data.get("field_confidence", 1.0)
        confidence_class = classify_field_confidence(fc, config)
        enriched_fields[field_name] = {**field_data, "confidence_class": confidence_class}

    return {**content, "fields": enriched_fields}
