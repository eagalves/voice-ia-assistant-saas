"""Tenant-aware metric label registry.

Issue #10. Every Prometheus metric exposed by the server MUST include
``tenant_id`` as a label so that Fase 3 multi-tenant analytics can slice
without a re-instrumentation pass. Issue #9 builds the actual exporter
and counters; this module exists today so anyone introducing a new
metric goes through one place that already knows the contract.
"""

from __future__ import annotations

REQUIRED_METRIC_LABELS: tuple[str, ...] = ("tenant_id",)


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
