from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from assistente_voz.conversation_state import ConversationState

# ── data contracts ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    latency_ms: float


# ── LLM backend interface (injectable) ────────────────────────────────────────


@runtime_checkable
class LLMBackend(Protocol):
    def complete(self, messages: list[dict]) -> tuple[str, str]:
        """Return (response_text, model_name)."""
        ...


# ── OpenAI production backend ─────────────────────────────────────────────────


class OpenAIBackend:
    """Production backend using openai SDK."""

    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self._model = model
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # deferred import

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def complete(self, messages: list[dict]) -> tuple[str, str]:
        client = self._get_client()
        response = client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        text = response.choices[0].message.content or ""
        return text, response.model


# ── system prompt builder ─────────────────────────────────────────────────────


def build_system_prompt(profile_data: dict) -> str:
    """Derive a system prompt from a loaded profile's YAML data."""
    profile = profile_data.get("profile", {})
    description = profile.get("description", "")
    name = profile.get("name", profile.get("id", "assistant"))

    lines: list[str] = [
        f"Você é {name}. {description}".strip(),
        "",
    ]

    # output contract sections → guardrails
    output_contract = profile_data.get("output_contract", {})
    required_sections = output_contract.get("required_sections", [])
    if required_sections:
        lines.append("Suas respostas devem sempre incluir as seguintes seções:")
        for sec in required_sections:
            lines.append(f"  - {sec.replace('_', ' ').capitalize()}")
        lines.append("")

    # anamnesis rules
    anamnesis = profile_data.get("anamnesis", {})
    required_fields = anamnesis.get("required_fields", [])
    max_q = anamnesis.get("max_questions_per_cycle")
    if required_fields:
        lines.append("Campos obrigatórios a coletar na anamnese:")
        for f in required_fields:
            lines.append(f"  - {f.replace('_', ' ')}")
        lines.append("")
    if max_q:
        lines.append(f"Faça no máximo {max_q} perguntas por ciclo de interação.")
        lines.append("")

    # interview rules
    interview = profile_data.get("interview", {})
    seniority_weights = interview.get("seniority_weights", {})
    if seniority_weights:
        lines.append("Pesos de avaliação por senioridade estão configurados no perfil.")
        lines.append("")

    # safety / red flags
    stt_cfg = profile_data.get("stt", {})
    if stt_cfg.get("block_llm_below_threshold"):
        lines.append(
            "Nunca prossiga com informação cuja transcrição não foi confirmada."
        )
        lines.append("")

    # generic extra instructions from profile
    instructions = profile_data.get("instructions", "")
    if instructions:
        lines.append(instructions.strip())
        lines.append("")

    return "\n".join(lines).strip()


# ── LLM service ────────────────────────────────────────────────────────────────


class LLMService:
    """Send conversation history to the LLM using the active profile's prompt.

    Usage::

        svc = LLMService(backend=OpenAIBackend(api_key="..."), profile_data={...})
        svc.apply_profile(state)        # sets system message from profile
        response = svc.complete(state)  # sends history, returns LLMResponse
    """

    def __init__(
        self,
        backend: LLMBackend,
        profile_data: dict,
        max_retries: int = 2,
        retry_delay_s: float = 1.0,
    ) -> None:
        self._backend = backend
        self._profile_data = profile_data
        self._max_retries = max_retries
        self._retry_delay = retry_delay_s

    def apply_profile(self, state: ConversationState) -> None:
        """Inject the profile's system prompt into *state*."""
        system_prompt = build_system_prompt(self._profile_data)
        state.set_system(system_prompt)

    def complete(self, state: ConversationState) -> LLMResponse:
        """Call the LLM with the current conversation history.

        Retries on transient errors up to ``max_retries`` times.
        Raises the last exception if all retries fail.
        """
        messages = state.to_llm_messages()
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 2):
            t0 = time.perf_counter()
            try:
                text, model = self._backend.complete(messages)
                latency_ms = (time.perf_counter() - t0) * 1000
                return LLMResponse(text=text, model=model, latency_ms=latency_ms)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt <= self._max_retries:
                    time.sleep(self._retry_delay)

        raise last_exc  # type: ignore[misc]

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, profile_data: dict) -> "LLMService":
        """Build LLMService from environment variables.

        LLM_API_KEY   — OpenAI API key (required when LLM_PROVIDER=api)
        LLM_MODEL     — model name (default: gpt-4o)
        LLM_RETRIES   — max retries (default: 2)
        """
        api_key = os.environ["LLM_API_KEY"]
        model = os.getenv("LLM_MODEL", "gpt-4o")
        max_retries = int(os.getenv("LLM_RETRIES", "2"))
        return cls(
            backend=OpenAIBackend(api_key=api_key, model=model),
            profile_data=profile_data,
            max_retries=max_retries,
        )
