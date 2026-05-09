# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local Portuguese (pt-BR) voice assistant MVP. Captures microphone audio, transcribes locally with Whisper, sends context to a remote LLM (OpenAI/Gemini) or local Ollama, and returns text. Domain behaviour (medical anamnesis, clinical interviewer, generic interviewer, etc.) is encapsulated as YAML profiles loaded at boot — the core never references domain rules.

Authoritative documents (read these before non-trivial work):
- `PRD.md` — requirements, user stories, SLOs, scope.
- `arquitetura-assistente-voz-mvp.md` — module contracts and post-MVP additions.
- `ISSUES_MVP.md` — backlog and per-issue acceptance criteria; tests are named after issues.
- `profiles/README.md` — profile contract, error codes, validation order.
- `docs/latency_presets.md` — recommended `.env` blocks per latency/quality target.

## Common commands

Windows + PowerShell environment; the venv lives in `.venv\Scripts\`. `pyproject.toml` sets `pythonpath = ["src"]` so `import assistente_voz...` works under pytest without an editable install.

```powershell
# Full test suite (quiet)
.venv\Scripts\python.exe -m pytest -q

# Single test file (tests are organised per issue)
.venv\Scripts\python.exe -m pytest tests/test_issue27_llm_streaming.py -q

# Single test by node id
.venv\Scripts\python.exe -m pytest tests/test_issue6_llm.py::TestLLMService::test_streaming_emits_tokens -q

# Bootstrap smoke test (validates env + profile catalog)
.venv\Scripts\python.exe -m assistente_voz.main

# Voice CLI — final-only completude mode (default)
.venv\Scripts\python.exe -m assistente_voz.voice_cli --duration 60

# Voice CLI — bypass STT, send text straight to the LLM
.venv\Scripts\python.exe -m assistente_voz.voice_cli --text "Estou com dor de cabeca forte"

# Voice CLI — list mic devices and exit
.venv\Scripts\python.exe -m assistente_voz.voice_cli --list-devices
```

The app expects `.env` to exist (copy from `.env.example`). At minimum: `ASSISTANT_PROFILE` and `LLM_PROVIDER`; provider-specific keys (`LLM_API_KEY`, `GEMINI_API_KEY`) are required only when that provider is primary or fallback.

## Architecture

Core flow (`src/assistente_voz/`):
```
MicrophoneSource → live_caption (VAD/segmenter) → STTService → ConversationState → LLMService → TextUI/voice_cli
                                                                       ↑
                                                                  profile rules
```

Eight cohesive modules with thin contracts; the `VoiceOrchestrator` (`orchestrator.py`) only knows the abstract `STTService`/`LLMService`/`ConversationState` contracts and never imports a concrete backend.

Key invariants — break these only with deliberate intent:
- **Profile is the only domain switch.** Selection is by exact match between `ASSISTANT_PROFILE` and `profile.id` inside the YAML; **filename is not authoritative**. Loader validates each YAML against `profiles/schema/profile.schema.json` and rejects duplicate ids.
- **Provider is selected by config, not code.** `LLM_PROVIDER` ∈ {`openai`, `gemini`, `ollama`, `api`, `local`}. Adding a backend means implementing the `LLMBackend` protocol (`llm.py`) — do not branch in the orchestrator.
- **Stable error codes everywhere.** `PROFILE_ENV_MISSING`, `PROFILE_NOT_FOUND`, `PROFILE_YAML_INVALID`, `PROFILE_SCHEMA_INVALID`, `PROFILE_ID_DUPLICATE`, `PROFILE_PARAM_INVALID`, `CONFIG_ENV_MISSING`, `CONFIG_ENV_INVALID`, `STT_EMPTY`, `STT_LOW_CONFIDENCE`, `STT_EXCEPTION`, `LLM_ERR_UNAVAILABLE`, `LLM_ERR_LOGICAL`, `LLM_EXCEPTION`. Reuse — don't invent ad-hoc strings.
- **LLM retry/fallback is gated by error class.** Only `LLM_ERR_UNAVAILABLE` (connection refused, timeout, 5xx) is retried with exponential backoff + jitter and may trigger `LLM_FALLBACK_PROVIDER`. `LLM_ERR_LOGICAL` (auth, bad request) propagates immediately — never retry.
- **Orchestrator is fail-soft.** Exceptions from STT/LLM are caught and surfaced through `TurnResult.error_code` so the session stays alive. Don't let exceptions bubble up to the UI.
- **STT input contract.** `WhisperBackend.transcribe` accepts either int16 PCM bytes (with explicit `sample_rate` when ≠ 16 kHz) or float32 ndarrays already at 16 kHz. The adapter resamples to 16 kHz. A WARNING must be logged when capture rate differs from 16 kHz.
- **`medical` is a critical profile.** Final review of uncertain segments is mandatory; `STT_CONFIDENCE_BLOCK` cannot drop below 0.70; the orchestrator must run red-flag detection (`medical_rules.RED_FLAG_TERMS`) before emitting the final record.
- **Voice CLI runs three threads:** audio producer, VAD segmenter, STT worker, plus an LLM worker consuming a request queue so STT/capture never block on model calls.

## Profiles

Lives in `profiles/`, one YAML per profile. Required runtime params per profile family:
- `medical` → `MEDICAL_SESSION_MODE` ∈ {`consultation`, `doctor_followup`}.
- Anything matching `*interviewer*` (id contains `interviewer`) → `SENIORITY_LEVEL` ∈ {`junior`, `pleno`, `senior`}.

When adding a new profile: place `<profile_id>.yaml` in `profiles/`, ensure schema-conformant, add a section to `profiles/README.md`, and add an example block to `.env.example`. The bootstrap path `_validate_profile_params` in `bootstrap.py` is where new per-profile env contracts go.

## Tests

- Tests are organised one file per issue (`test_issueNN_<topic>.py`). When closing an issue, the matching test file should exist and pass.
- Tests assert externally observable behaviour (status codes, timings, output structure), not internal call shapes — keep this convention.
- Acceptance/SLO tests live in `test_issue13_acceptance_metrics.py`. WER ≤ 10% on the 50-frase battery, end-to-end p95 < 4 s, < 2% technical-error rate over 30 min.
- Run focused tests during development; the full suite before declaring an issue done.

## Git safety policy (project-specific)

Never run these without explicit user confirmation, even when they would close the task faster:
- Branch deletion: `branch -D`, `push --delete`.
- History rewrites on shared refs: `push --force`, `push --force-with-lease`, destructive `rebase` on a shared branch, `filter-branch`.
- Local destructive ops: `reset --hard`, `checkout -- <path>`, `clean -fd`, `stash drop`.

When in doubt about reversibility, stop and ask. This complements (does not override) the global rules.

## Known gotchas

- Windows mic sample rates often default to 44.1/48 kHz — `MicrophoneSource` falls back gracefully and logs the rate; if you bypass it (e.g. calling `STTService.transcribe` directly with raw bytes), pass `sample_rate` explicitly or accuracy will collapse silently.
- `WHISPER_MODEL` is the dominant STT latency lever. CPU-only `medium`/`large` will violate the p95 < 4 s SLO; switch to GPU or to `LLM_PROVIDER=openai|gemini` for the medical preset.
- The repo has nothing called `CLAUDE.md` historically — `xx.md` is an older draft of this same file; do not treat it as authoritative.
- Worktrees from `.claude/worktrees/` are gitignored; do not commit anything from there.
