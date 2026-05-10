"""OpenTelemetry tracing stub — Issue #9.

Provides a mock-friendly tracer that satisfies the same interface expected
by real OTel instrumentation.  Replace ``_Tracer`` / ``_Span`` with the
real ``opentelemetry.trace`` objects when the OTel SDK is available.

Usage::

    tracer = get_tracer()
    with tracer.start_as_current_span("submit_feedback") as span:
        span.set_attribute("session_id", str(session_id))
        span.set_attribute("tenant_id", tenant_id)
        ...

Testing::

    reset_tracer()   # fresh instance for isolation
    tracer = get_tracer()
    # ... do work ...
    assert tracer.spans[0].name == "submit_feedback"
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator


@dataclass
class _Span:
    """Single trace span (mock-friendly)."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _Tracer:
    """Mock OTel tracer that records spans in memory."""

    def __init__(self) -> None:
        self.spans: list[_Span] = []

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        **_kwargs: Any,
    ) -> Generator[_Span, None, None]:
        span = _Span(name=name)
        self.spans.append(span)
        yield span


# ── Singleton ─────────────────────────────────────────────────────────────────

_tracer: _Tracer = _Tracer()


def get_tracer() -> _Tracer:
    """Return the process-wide _Tracer instance."""
    return _tracer


def reset_tracer() -> None:
    """Replace the singleton with a fresh instance (test isolation)."""
    global _tracer
    _tracer = _Tracer()
