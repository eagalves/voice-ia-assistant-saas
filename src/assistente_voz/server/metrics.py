"""Tenant-aware metrics registry (Issues #7, #9, #10, #17).

Every metric exposed by the server carries a ``tenant_id`` label so that
Fase 3 multi-tenant analytics can slice without re-instrumentation.

Two layers coexist on purpose:

* ``_SimpleGauge`` / ``_SimpleHistogram`` / ``_SimpleCounter`` — tiny
  in-process stubs with a stable, mock-friendly API (``set/get``,
  ``observe/sum/count``, ``inc/value``). Tests and the DoD gate assert on
  them, so their API must stay stable.
* ``prometheus_client`` — when installed, every stub mirrors its value into
  a real collector in a dedicated ``CollectorRegistry`` exposed on
  ``GET /metrics`` (see ``render_metrics``). When the package is missing the
  stubs still work and ``render_metrics`` renders a minimal text exposition.

Default tenant label is the Fase 2 single-tenant UUID; pass ``tenant_id``
explicitly whenever the caller knows the tenant.
"""

from __future__ import annotations

from .config import DEFAULT_TENANT_ID

REQUIRED_METRIC_LABELS: tuple[str, ...] = ("tenant_id",)

try:  # optional dependency — stubs keep working without it
    from prometheus_client import (  # type: ignore[import-not-found]
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
    REGISTRY = CollectorRegistry()
except ImportError:  # pragma: no cover — exercised only without the package
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


_HISTOGRAM_BUCKETS_RATIO = (0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, float("inf"))
_HISTOGRAM_BUCKETS_MS = (100, 250, 500, 800, 1500, 3000, 5000, 10000, 15000, float("inf"))


class _SimpleGauge:
    """Single-value gauge (last value kept per process) with tenant label."""

    def __init__(self, name: str, documentation: str = "") -> None:
        self.name = name
        self.documentation = documentation or name
        self._value: float = 0.0
        self._prom = (
            Gauge(name, self.documentation, labelnames=REQUIRED_METRIC_LABELS, registry=REGISTRY)
            if PROMETHEUS_AVAILABLE
            else None
        )

    def set(self, value: float, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self._value = float(value)
        if self._prom is not None:
            self._prom.labels(**labels_for(tenant_id)).set(self._value)

    def get(self) -> float:
        return self._value


class _SimpleHistogram:
    """Append-only observation list with tenant label (mirror to Prometheus)."""

    def __init__(
        self,
        name: str,
        documentation: str = "",
        buckets: tuple[float, ...] = _HISTOGRAM_BUCKETS_RATIO,
    ) -> None:
        self.name = name
        self.documentation = documentation or name
        self._observations: list[float] = []
        self._prom = (
            Histogram(
                name,
                self.documentation,
                labelnames=REQUIRED_METRIC_LABELS,
                buckets=buckets,
                registry=REGISTRY,
            )
            if PROMETHEUS_AVAILABLE
            else None
        )

    def observe(self, value: float, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self._observations.append(float(value))
        if self._prom is not None:
            self._prom.labels(**labels_for(tenant_id)).observe(float(value))

    def sum(self) -> float:
        return sum(self._observations)

    def count(self) -> int:
        return len(self._observations)


class _SimpleCounter:
    """Monotonic counter with tenant label (mirror to Prometheus)."""

    def __init__(
        self, name: str, documentation: str = "", extra_labels: tuple[str, ...] = ()
    ) -> None:
        self.name = name
        self.documentation = documentation or name
        self._extra_labels = extra_labels
        self._values: dict[tuple, float] = {}
        self._prom = (
            Counter(
                name,
                self.documentation,
                labelnames=REQUIRED_METRIC_LABELS + extra_labels,
                registry=REGISTRY,
            )
            if PROMETHEUS_AVAILABLE
            else None
        )

    def inc(self, amount: float = 1.0, tenant_id: str = DEFAULT_TENANT_ID, **extra: str) -> None:
        labels = labels_for(tenant_id, **extra)
        key = tuple(sorted(labels.items()))
        self._values[key] = self._values.get(key, 0.0) + float(amount)
        if self._prom is not None:
            self._prom.labels(**labels).inc(float(amount))

    def value(self, tenant_id: str = DEFAULT_TENANT_ID, **extra: str) -> float:
        key = tuple(sorted(labels_for(tenant_id, **extra).items()))
        return self._values.get(key, 0.0)


# ── Metric instances ──────────────────────────────────────────────────────────

stt_circuit_state_gauge = _SimpleGauge(
    "stt_circuit_state", "0 = circuit closed (streaming), 1 = circuit open (batch fallback)"
)
"""0.0 = circuit closed (Deepgram healthy); 1.0 = circuit open (Whisper fallback)."""

draft_correction_ratio_histogram = _SimpleHistogram(
    "draft_correction_ratio", "chars_edited / chars_original per draft feedback"
)
"""Histogram of ``chars_edited / chars_original`` per feedback submission."""

draft_kill_switch_gauge = _SimpleGauge(
    "draft_kill_switch_active", "1 when draft generation kill switch is active"
)
"""1.0 when draft generation kill switch is active; 0.0 otherwise."""

events_late_arrival_ratio_gauge = _SimpleGauge(
    "events_late_arrival_ratio", "Fraction of audio frames flagged late_arrival (per process)"
)

sessions_active_gauge = _SimpleGauge(
    "sessions_active", "Open WebSocket audio connections on this instance"
)

stt_fallback_total = _SimpleCounter(
    "stt_fallback_total", "Chunks routed to batch fallback", extra_labels=("reason",)
)

stt_segment_duration_ms_histogram = _SimpleHistogram(
    "stt_segment_duration_ms",
    "Wall-clock ms to transcribe one audio segment",
    buckets=_HISTOGRAM_BUCKETS_MS,
)

audit_events_total = _SimpleCounter(
    "audit_events_total", "Audit log events appended", extra_labels=("event_type",)
)


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


def render_metrics() -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for ``GET /metrics``."""
    if PROMETHEUS_AVAILABLE and REGISTRY is not None:
        return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
    # Minimal exposition without prometheus_client (gauges only).
    lines = []
    for gauge in (
        stt_circuit_state_gauge,
        draft_kill_switch_gauge,
        events_late_arrival_ratio_gauge,
        sessions_active_gauge,
    ):
        lines.append(f"# TYPE {gauge.name} gauge")
        lines.append(f'{gauge.name}{{tenant_id="{DEFAULT_TENANT_ID}"}} {gauge.get()}')
    return ("\n".join(lines) + "\n").encode(), CONTENT_TYPE_LATEST
