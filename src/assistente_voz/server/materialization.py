"""MaterializationWorker — Issue #5 (+ #8, #9, #12, #14).

Transitions a draft from ``pending`` → ``ready`` with Postgres advisory-lock
based exactly-once semantics and automatic retry (3 attempts, exponential
backoff).

Usage (background task)::

    worker = MaterializationWorker(
        tenant_id=ctx.tenant_id,
        session_id=ctx.session_id,
        session_kind="medical",
    )
    background_tasks.add_task(worker.run)

The worker creates its own ``AsyncSession`` via ``get_sessionmaker()`` so it
does not hold the HTTP request session open.

``content_builder`` is injectable for tests. The default builder reads the
Sessão's Redis Stream (frames + ``transcript`` events written by the
``SessionTranscriber``) and produces the draft content:

* ``transcript`` — ordered segments (speaker, text, confidence, uncertain)
  and a speaker-tagged plain-text rendering with ``[incerto]`` prefixes;
* ``red_flags`` — ``medical`` sessions only, detected over the transcript
  with the MVP ``medical_rules`` list (ADR 0001: núcleo preservado);
* ``fields`` — per-field extraction is the LLM step (Issue #15, not yet
  wired); the key exists so ``field_confidence`` enrichment keeps its
  contract;
* ``draft_generation`` — ``transcript_only`` or
  ``disabled_by_kill_switch`` (Issue #9: transcription never stops).

Raw frame entries are **not** copied into the draft (a 1h Sessão has
~180k of them) — only counted.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from assistente_voz.medical_rules import detect_red_flags

from .audit_log import EVENT_DRAFT_FAILED, EVENT_DRAFT_MATERIALIZED, log_event
from .db.session import get_sessionmaker
from .event_stream import EventStreamClient
from .field_confidence import enrich_draft_content, get_field_confidence_config
from .kill_switch import get_kill_switch
from .redis_keys import session_events_key
from .repositories.drafts_repo import DraftsRepository
from .stream_client import get_stream_client
from .stt_service import apply_incerto_prefix

logger = logging.getLogger(__name__)

# Retry policy
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0  # attempt n waits BACKOFF_BASE_S * 2**(n-1) seconds

DRAFT_SCHEMA_VERSION: int = 1
GENERATION_TRANSCRIPT_ONLY = "transcript_only"
GENERATION_DISABLED_KILL_SWITCH = "disabled_by_kill_switch"

ContentBuilder = Callable[[str, str, EventStreamClient], Awaitable[dict]]


class _DraftNotFound(RuntimeError):
    """Draft row not visible yet (close transaction not committed) — retried."""


def _as_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def build_transcript(entries: list[tuple[str, dict[str, str]]]) -> dict[str, Any]:
    """Collapse stream entries into the ``transcript`` block of a draft.

    Only entries with ``type == "transcript"`` carry text; they are ordered
    by ``server_timestamp_ms`` (the dialogue reference clock) with the stream
    ID as tie-breaker. Frames are counted, never copied.
    """
    segments: list[dict[str, Any]] = []
    frame_count = 0
    for stream_id, fields in entries:
        entry_type = fields.get("type", "frame")
        if entry_type != "transcript":
            if entry_type == "frame":
                frame_count += 1
            continue
        text = (fields.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(fields.get("confidence", "0") or 0.0)
        except ValueError:
            confidence = 0.0
        try:
            timestamp_ms = int(fields.get("server_timestamp_ms", "0") or 0)
        except ValueError:
            timestamp_ms = 0
        segments.append(
            {
                "event_id": fields.get("event_id", ""),
                "speaker": fields.get("speaker") or "unknown",
                "text": text,
                "confidence": round(confidence, 4),
                "uncertain": _as_bool(fields.get("uncertain")),
                "timestamp_ms": timestamp_ms,
                "_stream_id": stream_id,
            }
        )

    segments.sort(key=lambda s: (s["timestamp_ms"], s["_stream_id"]))
    for seg in segments:
        seg.pop("_stream_id", None)

    lines = [
        f"{seg['speaker']}: "
        + (apply_incerto_prefix(seg["text"]) if seg["uncertain"] else seg["text"])
        for seg in segments
    ]
    return {
        "segments": segments,
        "text": "\n".join(lines),
        "segment_count": len(segments),
        "uncertain_count": sum(1 for s in segments if s["uncertain"]),
        "frame_count": frame_count,
    }


def make_default_content_builder(session_kind: str | None) -> ContentBuilder:
    """Return the default builder, parameterised by the Sessão's ``session_kind``."""

    async def _builder(
        tenant_id: str,
        session_id: str,
        stream_client: EventStreamClient,
    ) -> dict:
        key = session_events_key(tenant_id, session_id)
        entries = await stream_client.xrange(key)
        transcript = build_transcript(entries)

        kill_switch_active = get_kill_switch().is_active
        content: dict[str, Any] = {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "session_kind": session_kind,
            "draft_generation": (
                GENERATION_DISABLED_KILL_SWITCH
                if kill_switch_active
                else GENERATION_TRANSCRIPT_ONLY
            ),
            "transcript": transcript,
            "fields": {},
            "event_count": len(entries),
            "materialized_at": datetime.now(timezone.utc).isoformat(),
        }
        if session_kind == "medical":
            content["red_flags"] = detect_red_flags(transcript["text"])
        return enrich_draft_content(content, get_field_confidence_config())

    return _builder


class MaterializationWorker:
    """Runs the draft materialization pipeline with retry and advisory lock."""

    def __init__(
        self,
        *,
        tenant_id: str,
        session_id: str,
        stream_client: EventStreamClient | None = None,
        content_builder: ContentBuilder | None = None,
        session_kind: str | None = None,
        actor_sub: str | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.session_kind = session_kind
        self.actor_sub = actor_sub
        # Default to the process-wide client: the worker must read the same
        # stream the AudioGateway wrote to (a fresh FakeEventStream is empty).
        self._stream_client: EventStreamClient = stream_client or get_stream_client()
        self._content_builder: ContentBuilder = (
            content_builder or make_default_content_builder(session_kind)
        )
        self._last_summary: dict[str, Any] = {}

    async def run(self) -> None:
        """Execute materialization with up to MAX_ATTEMPTS retries.

        On total failure marks the draft as ``failed`` and logs at ERROR.
        Exits silently if the draft is already ``ready`` (concurrent close +
        auto-finalize race).
        """
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
            try:
                ok = await self._try_once()
                if ok:
                    logger.info(
                        "Draft materialized: tenant=%s session=%s attempt=%d",
                        self.tenant_id,
                        self.session_id,
                        attempt + 1,
                    )
                # ok=False: advisory lock acquired but status != pending —
                # another worker already finished; exit silently.
                return
            except _DraftNotFound as exc:
                last_exc = exc
                logger.warning(
                    "Materialization attempt %d/%d: draft row not found yet: tenant=%s session=%s",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    self.tenant_id,
                    self.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Materialization attempt %d/%d failed: tenant=%s session=%s error=%s",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    self.tenant_id,
                    self.session_id,
                    exc,
                )

        if isinstance(last_exc, _DraftNotFound):
            # No row to mark as failed — the close transaction never landed.
            logger.error(
                "Draft materialization aborted: no draft row after %d attempts: tenant=%s session=%s",
                MAX_ATTEMPTS,
                self.tenant_id,
                self.session_id,
            )
            return

        # All attempts exhausted — mark failed.
        await self._mark_failed(last_exc)

    async def _try_once(self) -> bool:
        """One materialization attempt inside its own DB transaction.

        Returns:
            True  — successfully transitioned to ready.
            False — draft not pending; exit silently.

        Raises ``_DraftNotFound`` when the draft row does not exist (yet).
        """
        content = await self._content_builder(
            self.tenant_id, self.session_id, self._stream_client
        )

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as db:
            async with db.begin():
                repo = DraftsRepository(db, self.tenant_id)
                result = await repo.materialize_with_lock(
                    uuid.UUID(self.session_id), content
                )
                if result is None:
                    raise _DraftNotFound(self.session_id)
                if result:
                    transcript = content.get("transcript") or {}
                    await log_event(
                        db,
                        tenant_id=self.tenant_id,
                        event_type=EVENT_DRAFT_MATERIALIZED,
                        session_id=self.session_id,
                        actor_sub=self.actor_sub,
                        payload={
                            "draft_generation": content.get("draft_generation"),
                            "event_count": content.get("event_count"),
                            "segment_count": transcript.get("segment_count"),
                            "uncertain_count": transcript.get("uncertain_count"),
                            "red_flag_count": len(content.get("red_flags") or []),
                        },
                    )
                return bool(result)

    async def _mark_failed(self, exc: Exception | None) -> None:
        """Mark draft as failed after all retries are exhausted."""
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as db:
                async with db.begin():
                    repo = DraftsRepository(db, self.tenant_id)
                    draft = await repo.get_by_session(uuid.UUID(self.session_id))
                    if draft is not None and draft.draft_status != "ready":
                        await repo.force_status(
                            draft,
                            status="failed",
                            partial=True,
                            last_error=str(exc) if exc else "Unknown error",
                        )
                        await log_event(
                            db,
                            tenant_id=self.tenant_id,
                            event_type=EVENT_DRAFT_FAILED,
                            session_id=self.session_id,
                            actor_sub=self.actor_sub,
                            payload={
                                "attempts": MAX_ATTEMPTS,
                                "error": type(exc).__name__ if exc else "Unknown",
                            },
                        )
        except Exception as mark_exc:  # noqa: BLE001
            logger.error(
                "Failed to mark draft as failed: tenant=%s session=%s error=%s",
                self.tenant_id,
                self.session_id,
                mark_exc,
            )

        logger.error(
            "Draft materialization FAILED after %d attempts: tenant=%s session=%s error=%s",
            MAX_ATTEMPTS,
            self.tenant_id,
            self.session_id,
            exc,
        )
