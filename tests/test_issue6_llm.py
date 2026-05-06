"""Issue 6 – LLM Remoto com Prompt Derivado do Perfil."""
import pytest

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import (
    LLMBackend,
    LLMResponse,
    LLMService,
    build_system_prompt,
)

# ── test doubles ──────────────────────────────────────────────────────────────


class StubBackend:
    """Returns a fixed response; records calls."""

    def __init__(self, response: str = "resposta do LLM", model: str = "stub-model") -> None:
        self._response = response
        self._model = model
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> tuple[str, str]:
        self.calls.append(messages)
        return self._response, self._model


class FailingBackend:
    """Raises on every call — for retry tests."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def complete(self, messages: list[dict]) -> tuple[str, str]:
        self.calls += 1
        raise self._error


class FailThenSucceedBackend:
    """Fails the first N calls, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0

    def complete(self, messages: list[dict]) -> tuple[str, str]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise ConnectionError("simulated timeout")
        return "recuperado com sucesso", "stub"


# ── fixtures ──────────────────────────────────────────────────────────────────

MEDICAL_PROFILE = {
    "profile": {"id": "medical", "description": "Assistente de anamnese médica."},
    "anamnesis": {
        "required_fields": ["queixa_principal", "inicio", "duracao"],
        "max_questions_per_cycle": 3,
    },
    "output_contract": {
        "required_sections": ["resumo_do_caso", "sinais_de_alerta"],
    },
    "stt": {"block_llm_below_threshold": True},
}

INTERVIEWER_PROFILE = {
    "profile": {"id": "clinical_pharmacist_interviewer", "description": "Entrevistador de farmácia clínica."},
    "interview": {"seniority_weights": {"junior": 1, "pleno": 2, "senior": 3}},
}

MINIMAL_PROFILE = {
    "profile": {"id": "minimal", "description": "Perfil mínimo."},
}


# ── build_system_prompt ───────────────────────────────────────────────────────


def test_system_prompt_includes_description():
    prompt = build_system_prompt(MEDICAL_PROFILE)
    assert "anamnese médica" in prompt


def test_system_prompt_includes_required_sections():
    prompt = build_system_prompt(MEDICAL_PROFILE)
    assert "resumo do caso" in prompt.lower()
    assert "sinais de alerta" in prompt.lower()


def test_system_prompt_includes_anamnesis_fields():
    prompt = build_system_prompt(MEDICAL_PROFILE)
    assert "queixa principal" in prompt.lower()


def test_system_prompt_includes_max_questions():
    prompt = build_system_prompt(MEDICAL_PROFILE)
    assert "3" in prompt


def test_system_prompt_changes_with_profile():
    medical = build_system_prompt(MEDICAL_PROFILE)
    interviewer = build_system_prompt(INTERVIEWER_PROFILE)
    assert medical != interviewer


# ── apply_profile ─────────────────────────────────────────────────────────────


def test_apply_profile_sets_system_message():
    state = ConversationState()
    svc = LLMService(backend=StubBackend(), profile_data=MEDICAL_PROFILE)
    svc.apply_profile(state)

    llm_msgs = state.to_llm_messages()
    assert llm_msgs[0]["role"] == "system"
    assert len(llm_msgs[0]["content"]) > 0


# ── complete ──────────────────────────────────────────────────────────────────


def test_complete_returns_response_with_history():
    state = ConversationState()
    state.add("user", "qual é a dose recomendada?")
    backend = StubBackend(response="500mg a cada 8 horas.")
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    response = svc.complete(state)

    assert response.text == "500mg a cada 8 horas."
    assert response.model == "stub-model"
    assert response.latency_ms >= 0
    assert len(backend.calls) == 1


def test_complete_sends_full_history_to_backend():
    state = ConversationState()
    state.set_system("Sistema de teste.")
    state.add("user", "pergunta")
    state.add("assistant", "resposta")
    state.add("user", "segunda pergunta")

    backend = StubBackend()
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    svc.complete(state)

    sent = backend.calls[0]
    assert sent[0]["role"] == "system"
    assert len(sent) == 4  # system + 3 messages


def test_complete_retries_on_transient_error():
    backend = FailThenSucceedBackend(fail_times=1)
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)
    state = ConversationState()
    state.add("user", "olá")

    response = svc.complete(state)

    assert response.text == "recuperado com sucesso"
    assert backend.calls == 2


def test_complete_raises_after_all_retries_fail():
    backend = FailingBackend(ConnectionError("offline"))
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)
    state = ConversationState()
    state.add("user", "olá")

    with pytest.raises(ConnectionError):
        svc.complete(state)

    assert backend.calls == 3  # 1 initial + 2 retries


def test_stub_backend_satisfies_protocol():
    assert isinstance(StubBackend(), LLMBackend)
