from pathlib import Path

import pytest

from assistente_voz.bootstrap import bootstrap_app
from assistente_voz.errors import ConfigError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSISTANT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("SENIORITY_LEVEL", raising=False)
    monkeypatch.delenv("MEDICAL_SESSION_MODE", raising=False)


def test_bootstrap_loads_active_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_PROFILE", "medical")
    monkeypatch.setenv("LLM_PROVIDER", "api")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    result = bootstrap_app(base_dir=Path.cwd())

    assert result.profile_id == "medical"
    assert result.profile_path.name == "medical.yaml"
    assert result.llm_provider == "api"


def test_bootstrap_fails_without_assistant_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "api")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    with pytest.raises(ConfigError) as exc:
        bootstrap_app(base_dir=Path.cwd())

    assert exc.value.code == "CONFIG_ENV_MISSING"
    assert "ASSISTANT_PROFILE" in exc.value.message


def test_bootstrap_fails_without_api_key_when_llm_provider_is_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_PROFILE", "medical")
    monkeypatch.setenv("LLM_PROVIDER", "api")

    with pytest.raises(ConfigError) as exc:
        bootstrap_app(base_dir=Path.cwd())

    assert exc.value.code == "CONFIG_ENV_MISSING"
    assert "LLM_API_KEY" in exc.value.message


def test_bootstrap_fails_with_invalid_seniority_level_for_interviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_PROFILE", "general_interviewer")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("SENIORITY_LEVEL", "staff")

    with pytest.raises(ConfigError) as exc:
        bootstrap_app(base_dir=Path.cwd())

    assert exc.value.code == "PROFILE_PARAM_INVALID"
    assert "SENIORITY_LEVEL" in exc.value.message


def test_bootstrap_fails_with_invalid_medical_session_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_PROFILE", "medical")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("MEDICAL_SESSION_MODE", "triage")

    with pytest.raises(ConfigError) as exc:
        bootstrap_app(base_dir=Path.cwd())

    assert exc.value.code == "PROFILE_PARAM_INVALID"
    assert "MEDICAL_SESSION_MODE" in exc.value.message
