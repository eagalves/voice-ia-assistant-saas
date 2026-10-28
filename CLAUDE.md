# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

pt-BR voice assistant component for a telemedicina SaaS. Domain behaviour is encapsulated as YAML profiles — the core never references domain rules directly. Fase 2 adds a FastAPI HTTP server consumed by the telemedicina app.

Authoritative documents (read these before non-trivial work):
- `PRD-fase2.md` — Fase 2 requirements and decisions (rev. 3 addendum at the end).
- `ISSUES_FASE2.md` — Fase 2 backlog; full technical specs with schemas, interfaces, invariants (#1–#22).
- `docs/adr/` — ADR 0001 (Fase 2/3 strategy) and 0002–0007 (STT local, LLM post-correction, AuditLog integrity, Redis protocol, WS auth binding, auto-finalize reaper).
- `PRD.md` — MVP requirements (voice pipeline).
- `ISSUES_MVP.md` — MVP backlog; per-issue acceptance criteria (1–31).
- `profiles/README.md` — profile contract, error codes, validation order.

## Common commands

Windows + PowerShell; venv at `.venv\Scripts\`. `pyproject.toml` sets `pythonpath = ["src"]`.
The package is installed editable from **this** directory (`pip install -e .`); if `python -c "import assistente_voz; print(assistente_voz.__file__)"` prints another path (an OneDrive copy once did), re-run `pip install -e .`.

```powershell
# Tests
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/fase2/test_issue002_idempotency.py -v
.venv\Scripts\python.exe -m pytest tests/fase2 -q                # needs Postgres (see below)
$env:TEST_REDIS_URL="redis://localhost:6379/15"; .venv\Scripts\python.exe -m pytest -m redis_real -q

# Fase 2 server (dev)
.venv\Scripts\python.exe -m assistente_voz.server
# OpenAPI contract (committed; CI drift check)
.venv\Scripts\python.exe -m assistente_voz.server.cli.export_openapi          # refresh
.venv\Scripts\python.exe -m assistente_voz.server.cli.export_openapi --check  # drift

# Alembic
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m alembic downgrade base
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "<description>"

# Voice CLI (MVP)
.venv\Scripts\python.exe -m assistente_voz.voice_cli --duration 60
.venv\Scripts\python.exe -m assistente_voz.voice_cli --text "Estou com dor de cabeca forte"

# STT: benchmark de modelos Whisper sobre fixtures pt-BR (WER/CER/RTF)
.venv\Scripts\python.exe scripts\benchmark_stt.py --models tiny,small,medium
.venv\Scripts\python.exe scripts\benchmark_stt.py --models small --ab-baseline  # config antiga p/ comparacao

# STT: regenerar fixtures de audio (Windows + voz pt-BR Microsoft Maria)
.venv\Scripts\python.exe scripts\generate_stt_fixtures.py

# STT: testes reais de transcricao (modelos de verdade; ~1 min)
.venv\Scripts\python.exe -m pytest tests/test_stt_real_transcription.py -v
# pular os lentos: -m "not stt_real" | incluir medium: STT_TEST_HEAVY=1
```

Fase 2 tests require Postgres. Set `TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/dbname` or have Docker for testcontainers (the conftest **drops and truncates tables** in that database — never point it at a shared DB). Redis is optional: `REDIS_URL` empty → in-memory `FakeEventStream`.

The app expects `.env` (copy from `.env.example`). At minimum: `ASSISTANT_PROFILE` and `LLM_PROVIDER`. Server knobs: `APP_ENV`, `JWT_JWKS_URL`, `REDIS_URL`, `STT_SERVER_BACKEND`, `SESSION_REAPER_INTERVAL_S`.

## Architecture

### Fase 2 HTTP server (`src/assistente_voz/server/`)
```
FastAPI app (app.py: lifespan = SIGTERM drain + SessionReaper; /health, /metrics)
├── middleware/        auth (BearerPresenceMiddleware; bypass only when JWT_JWKS_URL empty
│                      and APP_ENV not production), version header
├── routers/           sessions.py (create/close/token refresh), drafts.py (get/retry/feedback),
│                      audio.py (WS: token↔session binding, outbox, consumer → transcriber)
├── session_lifecycle  close_session_tx() — the ONLY open→closed transition (router + reaper)
├── reaper.py          auto-finalize past auto_finalize_at (every container, FOR UPDATE)
├── audio_gateway.py   frame queue, dedupe (speaker, seq) with uuid5 event_id, late-arrival,
│                      snapshots, consumer, record_transcript()
├── transcriber.py     SessionTranscriber: window/silence segmentation → STTStreamService →
│                      transcript events + WS messages (+ realtime red flags)
├── stt_service.py     STTStreamService + circuit breaker; backends fake | local_whisper
│                      (BatchOnlyStreamingBackend/LocalWhisperBatchBackend); build_stt_service()
├── materialization.py MaterializationWorker: stream → draft {transcript, red_flags, fields{}}
├── audit_log.py       hash chain (seq order, full-record hash, advisory lock, gap detection)
├── event_stream.py    EventStreamClient protocol; FakeEventStream (memory) | RedisEventStream
├── stream_client.py   get_stream_client() — REDIS_URL selects the implementation
├── repositories/      base.py (TenantRepository), sessions_repo (+SystemSessionsRepository),
│                      drafts_repo, feedback_repo, idempotency_repo
├── db/                models.py (Session, Draft, DraftFeedback, IdempotencyKey, AuditLog),
│                      session.py (async engine + override_engine test hook)
├── auth/              jwt_validator, jwks_cache (401 on unknown kid; refresh rate-limited), jti_cache
├── domain/            plans.py (plan features)
├── schemas/           Pydantic I/O models
├── config.py          ServerSettings (cached; reset_settings_cache() in tests)
├── metrics.py         stubs mirrored into prometheus_client; labels_for() — tenant_id required
├── kill_switch.py, field_confidence.py, drain.py, tracing.py (OTel stub), deps.py (DbSession)
└── redis_keys.py      session_events_key(), ws_token_key() — {tenant_id} hash tag required
```

Alembic chain: `0001_phase2_initial` → `0002_rls_policies` → `0003_idempotency_keys` → `0004_draft_status_index` → `0005_audit_log_hash_chain` → `0006_audit_log_seq_append_only`

Fase 2 tests: `tests/fase2/test_issueNNN_<topic>.py`. `conftest.py` runs `alembic upgrade head` (not `metadata.create_all`) with function-scoped NullPool engines.

### MVP voice pipeline (`src/assistente_voz/`)
`MicrophoneSource → live_caption (VAD) → STTService → ConversationState → LLMService → voice_cli`

Optional post-correction (`stt_correction.py`): an LLM proposes JSON edit-lists
(`de`→`para`) applied programmatically under guardrails (no paraphrase, no
deletion, word-boundary match, phonetic-similarity per edit, divergence cap,
fail-open). Enabled by `STT_POSTCORRECT_ENABLED`; quality eval helpers live in
`stt_eval.py` (WER/CER with pt-BR normalization). `medical_rules.detect_red_flags`
(accent-insensitive, whole-word) is shared with the server.

`VoiceOrchestrator` knows only abstract contracts; never imports a concrete backend. Profile is the only domain switch — selected by `ASSISTANT_PROFILE` matching `profile.id` inside the YAML (filename is not authoritative).

## Invariants

**Fase 2 server:**
- **Every DB query through `TenantRepository`.** `_scope_tenant(stmt, Model)` injects `WHERE tenant_id = :tid`. No module outside `repositories/` and `db/` may import `sqlalchemy.orm.Session` (AST lint). `SystemSessionsRepository` is the only sanctioned cross-tenant reader (reaper).
- **Routers inject the DB as `DbSession`** (`Depends(get_db_session, scope="function")`). A plain `Depends(get_db_session)` commits *after* the response and after BackgroundTasks — workers would not see the rows.
- **Every Redis key has `{tenant_id}` as hash tag.** Use `redis_keys.py` helpers — never inline.
- **Every Prometheus metric carries `tenant_id`.** Use `metrics.labels_for(tenant_id, ...)`; stubs take `tenant_id=`.
- **Idempotency at the router.** Scope: `(tenant_id, endpoint, actor_sub_jwt, idempotency_key)`. Same key + different payload → `409`. Same key + same payload → `201 Idempotency-Replayed: true`. Concurrent same key → one Session (loser rolls back and replays). TTL 24h.
- **`close_session_tx` is the only open→closed transition** (router and reaper); it revokes the token, expires the stream, inserts the pending draft and writes the audit event.
- **All WebSocket writes go through the connection outbox**; only the sender task touches the socket.
- **AuditLog is append-only and chained by `seq`**; hash covers `event_type, session_id, actor_sub, payload`. Never add fields to the hashed record without a re-hash migration + ADR.
- **All Alembic migrations must have a non-trivial `downgrade()`.** CI AST-scans this.

**MVP voice pipeline:**
- **Stable error codes.** `STT_EMPTY`, `STT_LOW_CONFIDENCE`, `STT_EXCEPTION`, `LLM_ERR_UNAVAILABLE`, `LLM_ERR_LOGICAL`, `LLM_EXCEPTION`, `PROFILE_*`, `CONFIG_*`, `CORRECTION_*` (CorrectionOutcome). Reuse — don't invent ad-hoc strings.
- **LLM retry only on `LLM_ERR_UNAVAILABLE`** (5xx/timeout), in `complete()` and in `stream()` before the first token. `LLM_ERR_LOGICAL` propagates immediately; mid-stream failures propagate.
- **`medical` profile:** `STT_CONFIDENCE_BLOCK` ≥ 0.70; red-flag detection mandatory before emitting final record.

## Profiles

`profiles/`, one YAML per profile. Required env per family: `medical` → `MEDICAL_SESSION_MODE`; `*interviewer*` → `SENIORITY_LEVEL` (and `seniority` in `POST /v1/sessions`). New profile: add YAML + `profiles/README.md` entry + `.env.example` block; param validation in `bootstrap.py::_validate_profile_params`.

## Tests

One file per issue (`test_issueNN_<topic>.py` / `test_issueNNN_<topic>.py`). Assert externally observable behaviour, not internal call shapes. Run focused during dev; full suite before closing an issue. Tests that must simulate AuditLog tampering disable `trg_audit_log_append_only` explicitly (as a DBA would).

After all tests for an issue pass, mark its acceptance criteria as done in `ISSUES_MVP.md` or `ISSUES_FASE2.md`: change `- [ ]` to `- [x]` for every criterion that has a passing test. Leave unchecked only criteria that require real external services (Deepgram, OpenAI) or manual sign-off (DPA, DPIA). Decisions that are hard to reverse, surprising without context and the result of a real trade-off get an ADR in `docs/adr/` (format: `.agents/skills/grill-with-docs/ADR-FORMAT.md`). Router changes require regenerating `schemas/openapi/v1.yaml`.

## Git safety policy

Never without explicit confirmation: `branch -D`, `push --force`, `reset --hard`, `checkout -- <path>`, `clean -fd`, `stash drop`, destructive rebase on shared branch.

## Known gotchas

- **`fileConfig(disable_existing_loggers=False)` in `migrations/env.py`** — omitting it silences pytest loggers and causes Phase 1 tests to fail silently.
- **`asyncio_mode=auto` in `pyproject.toml`** — do not add `pytestmark = pytest.mark.asyncio` to Fase 2 test modules; it triggers spurious warnings on sync functions.
- **`override_engine()` in `db/session.py`** is the test hook for NullPool engines. Never call `init_engine()` directly in tests.
- **FastAPI ≥ 0.121 yield dependencies default to `scope="request"`** (exit after the response and after BackgroundTasks). Use `DbSession` from `deps.py`.
- **BackgroundTasks finish before `AsyncClient`/`TestClient` return**, so a draft is already `ready` right after `POST /close` in tests; force `pending` explicitly to test the 202 path.
- **`get_settings()` is cached** — tests that change env vars call `reset_settings_cache()` (and restore it in `finally`).
- **`FakeEventStream` is per process** — `get_stream_client()` without `REDIS_URL` means no reconnection across containers; `/health` tells which one is active.
- **WebSocket `accept(headers=...)` takes `[(bytes, bytes)]`** — a dict "works" in TestClient and breaks under uvicorn.
- **Windows mic rates (44.1/48 kHz)** — pass `sample_rate` explicitly when calling `STTService.transcribe` with raw bytes (the orchestrator now does), or accuracy collapses silently.
- **Long heredocs in the Bash tool may fail with "unexpected EOF"** — write the content to a file and `cat` it instead.
