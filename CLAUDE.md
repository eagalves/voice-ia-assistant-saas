# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

pt-BR voice assistant component for a telemedicina SaaS. Domain behaviour is encapsulated as YAML profiles — the core never references domain rules directly. Fase 2 adds a FastAPI HTTP server consumed by the telemedicina app.

Authoritative documents (read these before non-trivial work):
- `PRD-fase2.md` — Fase 2 requirements and decisions.
- `ISSUES_FASE2.md` — Fase 2 backlog; full technical specs with schemas, interfaces, invariants.
- `PRD.md` — MVP requirements (voice pipeline).
- `ISSUES_MVP.md` — MVP backlog; per-issue acceptance criteria.
- `profiles/README.md` — profile contract, error codes, validation order.

## Common commands

Windows + PowerShell; venv at `.venv\Scripts\`. `pyproject.toml` sets `pythonpath = ["src"]`.

```powershell
# Tests
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/fase2/test_issue002_idempotency.py -v

# Fase 2 server (dev)
.venv\Scripts\python.exe -m assistente_voz.server

# Alembic
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m alembic downgrade base
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "<description>"

# Voice CLI (MVP)
.venv\Scripts\python.exe -m assistente_voz.voice_cli --duration 60
.venv\Scripts\python.exe -m assistente_voz.voice_cli --text "Estou com dor de cabeca forte"
```

Fase 2 tests require Postgres. Set `TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/dbname` or have Docker for testcontainers.

The app expects `.env` (copy from `.env.example`). At minimum: `ASSISTANT_PROFILE` and `LLM_PROVIDER`.

## Architecture

### Fase 2 HTTP server (`src/assistente_voz/server/`)
```
FastAPI app
├── middleware/   auth (BearerPresenceMiddleware), version header
├── routers/      sessions.py, drafts.py
├── repositories/ base.py (TenantRepository), sessions_repo, drafts_repo,
│                 feedback_repo, idempotency_repo
├── db/           models.py (Session, Draft, DraftFeedback, IdempotencyKey),
│                 session.py (async engine + override_engine test hook)
├── domain/       plans.py (plan features)
├── schemas/      Pydantic I/O models
├── context.py    SessionContext dataclass
├── config.py     ServerSettings (pydantic-settings)
├── metrics.py    labels_for() — tenant_id required on every metric
└── redis_keys.py session_events_key(), ws_token_key() — {tenant_id} hash tag required
```

Alembic chain: `0001_phase2_initial` → `0002_rls_policies` → `0003_idempotency_keys`

Fase 2 tests: `tests/fase2/test_issueNNN_<topic>.py`. `conftest.py` runs `alembic upgrade head` (not `metadata.create_all`) with function-scoped NullPool engines.

### MVP voice pipeline (`src/assistente_voz/`)
`MicrophoneSource → live_caption (VAD) → STTService → ConversationState → LLMService → voice_cli`

`VoiceOrchestrator` knows only abstract contracts; never imports a concrete backend. Profile is the only domain switch — selected by `ASSISTANT_PROFILE` matching `profile.id` inside the YAML (filename is not authoritative).

## Invariants

**Fase 2 server:**
- **Every DB query through `TenantRepository`.** `_scope_tenant(stmt, Model)` injects `WHERE tenant_id = :tid`. No module outside `repositories/` and `db/` may import `AsyncSession` directly (AST lint).
- **Every Redis key has `{tenant_id}` as hash tag.** Use `redis_keys.py` helpers — never inline.
- **Every Prometheus metric carries `tenant_id`.** Use `metrics.labels_for(tenant_id, ...)`.
- **Idempotency at the router.** Scope: `(tenant_id, endpoint, actor_sub_jwt, idempotency_key)`. Same key + different payload → `409`. Same key + same payload → `201 Idempotency-Replayed: true`. TTL 24h.
- **All Alembic migrations must have a non-trivial `downgrade()`.** CI AST-scans this.

**MVP voice pipeline:**
- **Stable error codes.** `STT_EMPTY`, `STT_LOW_CONFIDENCE`, `STT_EXCEPTION`, `LLM_ERR_UNAVAILABLE`, `LLM_ERR_LOGICAL`, `LLM_EXCEPTION`, `PROFILE_*`, `CONFIG_*`. Reuse — don't invent ad-hoc strings.
- **LLM retry only on `LLM_ERR_UNAVAILABLE`** (5xx/timeout). `LLM_ERR_LOGICAL` propagates immediately.
- **`medical` profile:** `STT_CONFIDENCE_BLOCK` ≥ 0.70; red-flag detection mandatory before emitting final record.

## Profiles

`profiles/`, one YAML per profile. Required env per family: `medical` → `MEDICAL_SESSION_MODE`; `*interviewer*` → `SENIORITY_LEVEL`. New profile: add YAML + `profiles/README.md` entry + `.env.example` block; param validation in `bootstrap.py::_validate_profile_params`.

## Tests

One file per issue (`test_issueNN_<topic>.py` / `test_issueNNN_<topic>.py`). Assert externally observable behaviour, not internal call shapes. Run focused during dev; full suite before closing an issue.

## Git safety policy

Never without explicit confirmation: `branch -D`, `push --force`, `reset --hard`, `checkout -- <path>`, `clean -fd`, `stash drop`, destructive rebase on shared branch.

## Known gotchas

- **`fileConfig(disable_existing_loggers=False)` in `migrations/env.py`** — omitting it silences pytest loggers and causes Phase 1 tests to fail silently.
- **`asyncio_mode=auto` in `pyproject.toml`** — do not add `pytestmark = pytest.mark.asyncio` to Fase 2 test modules; it triggers spurious warnings on sync functions.
- **`override_engine()` in `db/session.py`** is the test hook for NullPool engines. Never call `init_engine()` directly in tests.
- **Windows mic rates (44.1/48 kHz)** — pass `sample_rate` explicitly when calling `STTService.transcribe` with raw bytes, or accuracy collapses silently.


