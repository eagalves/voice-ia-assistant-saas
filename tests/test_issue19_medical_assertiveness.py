"""Issue 19 - Regressao de assertividade do perfil medical."""

from __future__ import annotations

from pathlib import Path

import yaml

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMService, build_system_prompt


class _CaptureBackend:
    """Test double that captures outbound messages."""

    provider_name = "capture"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> tuple[str, str, str]:
        self.calls.append(messages)
        return "ok", "capture-model", self.provider_name


def _load_medical_profile() -> dict:
    profile_path = Path(__file__).resolve().parents[1] / "profiles" / "medical.yaml"
    with profile_path.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)


def test_medical_profile_contains_required_assertiveness_rules() -> None:
    profile = _load_medical_profile()
    instructions = str(profile.get("instructions", "")).lower()

    assert "nao inferir fatos" in instructions
    assert "ambiguidade" in instructions
    assert "fazer uma pergunta por vez" in instructions
    assert "se houver incerteza, declarar incerteza" in instructions


def test_medical_profile_contains_explicit_ambiguous_accident_guidance() -> None:
    profile = _load_medical_profile()
    instructions = str(profile.get("instructions", "")).lower()

    assert "que tipo de acidente foi" in instructions
    assert "nao assumir acidente de transito" in instructions


def test_build_system_prompt_includes_assertiveness_clarification_contract() -> None:
    profile = _load_medical_profile()

    prompt = build_system_prompt(profile).lower()

    assert "nao inferir fatos" in prompt
    assert "pedir esclarecimento" in prompt
    assert "fazer uma pergunta por vez" in prompt


def test_llm_service_applies_system_prompt_with_anti_inference_rules() -> None:
    profile = _load_medical_profile()
    backend = _CaptureBackend()
    service = LLMService(backend=backend, profile_data=profile)

    state = ConversationState()
    state.add("user", "sofri um acidente ontem")
    service.apply_profile(state)
    service.complete(state)

    sent = backend.calls[0]
    assert sent[0]["role"] == "system"

    system_prompt = str(sent[0]["content"]).lower()
    assert "que tipo de acidente foi" in system_prompt
    assert "nao assumir acidente de transito" in system_prompt
    assert "fazer uma pergunta por vez" in system_prompt
