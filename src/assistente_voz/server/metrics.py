"""Tenant-aware metric label registry.

Issue #10. Every Prometheus metric exposed by the server MUST include
``tenant_id`` as a label so that Fase 3 multi-tenant analytics can slice
without a re-instrumentation pass. Issue #9 builds the actual exporter
and counters; this module exists today so anyone introducing a new
metric goes through one place that already knows the contract.

Issue #7 adds ``stt_circuit_state_gauge`` — a mock-friendly stub that can
be swapped for a real ``prometheus_client.Gauge`` in production with a
one-liner replacement of ``_SimpleGauge``.

Issue #9 adds ``draft_correction_ratio_histogram`` and
``draft_kill_switch_gauge``.
"""

from __future__ import annotations

REQUIRED_METRIC_LABELS: tuple[str, ...] = ("tenant_id",)


# ── Mock-friendly metric stubs ────────────────────────────────────────────────
# These satisfy the interface expected by production code and tests without
# requiring prometheus_client to be installed.  When prometheus_client is
# available, replace ``_SimpleGauge`` / ``_SimpleHistogram`` with the real
# Gauge / Histogram classes and the rest of the codebase stays unchanged.


class _SimpleGauge:
    """Single-value gauge stub (no labels, no prometheus_client dependency)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._value: float = 0.0

    def set(self, value: float) -> None:
        self._value = value

    def get(self) -> float:
        return self._value


class _SimpleHistogram:
    """Append-only observation stub (no prometheus_client dependency)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._observations: list[float] = []

    def observe(self, value: float) -> None:
        self._observations.append(value)

    def sum(self) -> float:
        return sum(self._observations)

    def count(self) -> int:
        return len(self._observations)


# ── Metric instances ──────────────────────────────────────────────────────────

stt_circuit_state_gauge = _SimpleGauge("stt_circuit_state")
"""0.0 = circuit closed (Deepgram healthy); 1.0 = circuit open (Whisper fallback)."""

draft_correction_ratio_histogram = _SimpleHistogram("draft_correction_ratio")
"""Histogram of ``chars_edited / chars_original`` per feedback submission."""

draft_kill_switch_gauge = _SimpleGauge("draft_kill_switch_active")
"""1.0 when draft generation kill switch is active; 0.0 otherwise."""


def labels_for(tenant_id: str, **extra: str) -> dict[str, str]:
    """Build a label dict that always carries ``tenant_id``.

    Use::

        SOME_COUNTER.labels(**labels_for(tenant_id, route="/v1/sessions"))
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for every metric label set.")
    payload = {"tenant_id": tenant_id}
    for key, value in extra.items():
        if key == "tenant_id":
            raise ValueError(
                "tenant_id must come from the positional argument, not extras."
            )
        payload[key] = value
    return payload


def assert_labels_include_tenant(label_names: tuple[str, ...]) -> None:
    """Helper for tests / metric registries to assert tenant_id is present."""
    missing = [name for name in REQUIRED_METRIC_LABELS if name not in label_names]
    if missing:
        raise AssertionError(
            f"Metric is missing required label(s): {missing}. "
            "Use `metrics.labels_for(tenant_id, ...)` to build the label set."
        )
