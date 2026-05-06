from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from assistente_voz.audio_capture import AudioSegment
from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMService
from assistente_voz.stt import STTService, TranscriptionStatus


class MetricsSink(Protocol):
    """Simple metrics contract used by the orchestrator."""

    def observe(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        ...


class NullMetrics:
    """No-op metrics sink used by default."""

    def observe(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        return None


@dataclass(frozen=True)
class TurnResult:
    """External behaviour contract for one processed audio segment."""

    ok: bool
    transcription: str | None
    assistant_response: str | None
    stt_status: str
    stt_attempts: int
    stt_confidence: float | None
    stt_latency_ms: float
    llm_latency_ms: float | None
    e2e_latency_ms: float
    error_code: str | None = None


class VoiceOrchestrator:
    """Connect audio -> STT -> conversation state -> LLM with fail-soft errors."""

    def __init__(
        self,
        stt: STTService,
        llm: LLMService,
        state: ConversationState,
        metrics: MetricsSink | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._stt = stt
        self._llm = llm
        self._state = state
        self._metrics = metrics or NullMetrics()
        self._logger = logger or logging.getLogger("assistente_voz.orchestrator")

        # Ensure profile-derived system prompt is always present before first turn.
        self._llm.apply_profile(self._state)

    def process_segment(self, segment: AudioSegment) -> TurnResult:
        """Process one segment end-to-end, returning stable status and timings.

        This method is fail-soft: errors are returned in TurnResult instead of
        bubbling up and terminating the session.
        """
        e2e_start = time.perf_counter()

        stt_latency_ms = 0.0
        llm_latency_ms: float | None = None

        # ---- STT stage ----
        stt_start = time.perf_counter()
        try:
            stt_outcome = self._stt.transcribe(segment.audio_bytes)
        except Exception as exc:  # noqa: BLE001
            stt_latency_ms = (time.perf_counter() - stt_start) * 1000
            e2e_latency_ms = (time.perf_counter() - e2e_start) * 1000
            self._metrics.observe("stt.latency_ms", stt_latency_ms)
            self._metrics.observe("e2e.latency_ms", e2e_latency_ms)
            self._logger.error("module=stt code=STT_EXCEPTION err=%s", type(exc).__name__)
            return TurnResult(
                ok=False,
                transcription=None,
                assistant_response=None,
                stt_status="ERROR",
                stt_attempts=0,
                stt_confidence=None,
                stt_latency_ms=stt_latency_ms,
                llm_latency_ms=None,
                e2e_latency_ms=e2e_latency_ms,
                error_code="STT_EXCEPTION",
            )

        stt_latency_ms = (time.perf_counter() - stt_start) * 1000
        self._metrics.observe("stt.latency_ms", stt_latency_ms)

        if stt_outcome.status == TranscriptionStatus.EMPTY:
            e2e_latency_ms = (time.perf_counter() - e2e_start) * 1000
            self._metrics.observe("e2e.latency_ms", e2e_latency_ms)
            self._logger.warning("module=stt code=STT_EMPTY")
            return TurnResult(
                ok=False,
                transcription=None,
                assistant_response=None,
                stt_status="EMPTY",
                stt_attempts=stt_outcome.attempts,
                stt_confidence=None,
                stt_latency_ms=stt_latency_ms,
                llm_latency_ms=None,
                e2e_latency_ms=e2e_latency_ms,
                error_code="STT_EMPTY",
            )

        if stt_outcome.status == TranscriptionStatus.LOW_CONFIDENCE:
            e2e_latency_ms = (time.perf_counter() - e2e_start) * 1000
            self._metrics.observe("e2e.latency_ms", e2e_latency_ms)
            conf = stt_outcome.result.confidence if stt_outcome.result else None
            self._logger.warning("module=stt code=STT_LOW_CONFIDENCE")
            return TurnResult(
                ok=False,
                transcription=stt_outcome.result.text if stt_outcome.result else None,
                assistant_response=None,
                stt_status="LOW_CONFIDENCE",
                stt_attempts=stt_outcome.attempts,
                stt_confidence=conf,
                stt_latency_ms=stt_latency_ms,
                llm_latency_ms=None,
                e2e_latency_ms=e2e_latency_ms,
                error_code="STT_LOW_CONFIDENCE",
            )

        # ---- state update ----
        assert stt_outcome.result is not None
        self._state.add(role="user", content=stt_outcome.result.text)

        # ---- LLM stage ----
        llm_start = time.perf_counter()
        try:
            llm_response = self._llm.complete(self._state)
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000
        except Exception as exc:  # noqa: BLE001
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000
            e2e_latency_ms = (time.perf_counter() - e2e_start) * 1000
            self._metrics.observe("llm.latency_ms", llm_latency_ms)
            self._metrics.observe("e2e.latency_ms", e2e_latency_ms)
            self._logger.error("module=llm code=LLM_EXCEPTION err=%s", type(exc).__name__)
            return TurnResult(
                ok=False,
                transcription=stt_outcome.result.text,
                assistant_response=None,
                stt_status="OK",
                stt_attempts=stt_outcome.attempts,
                stt_confidence=stt_outcome.result.confidence,
                stt_latency_ms=stt_latency_ms,
                llm_latency_ms=llm_latency_ms,
                e2e_latency_ms=e2e_latency_ms,
                error_code="LLM_EXCEPTION",
            )

        self._metrics.observe("llm.latency_ms", llm_latency_ms)

        # ---- state update (assistant reply) ----
        self._state.add(role="assistant", content=llm_response.text)

        e2e_latency_ms = (time.perf_counter() - e2e_start) * 1000
        self._metrics.observe("e2e.latency_ms", e2e_latency_ms)

        return TurnResult(
            ok=True,
            transcription=stt_outcome.result.text,
            assistant_response=llm_response.text,
            stt_status="OK",
            stt_attempts=stt_outcome.attempts,
            stt_confidence=stt_outcome.result.confidence,
            stt_latency_ms=stt_latency_ms,
            llm_latency_ms=llm_latency_ms,
            e2e_latency_ms=e2e_latency_ms,
            error_code=None,
        )
