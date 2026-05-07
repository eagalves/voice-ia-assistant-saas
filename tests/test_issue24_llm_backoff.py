"""Issue 24 – Backoff Exponencial nos Retries de LLM.

Garante que:
- _compute_backoff aplica formula 2^(retry-1) com cap e jitter.
- Primeira retry aguarda ~base_s, segunda ~2*base_s.
- Cap e respeitado: nenhum sleep maior que backoff_max_s.
- Jitter esta dentro de +/-20% do delay base.
- LLMService dorme o intervalo calculado entre retries de UNAVAILABLE.
- Erro logico nao gera sleep.
- Log registra o intervalo aguardado em cada retry.
- from_env le LLM_RETRY_BACKOFF_BASE_S e LLM_RETRY_BACKOFF_MAX_S.
"""
from __future__ import annotations

import logging
from unittest.mock import call, patch

import pytest

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import (
    LLMErrorCode,
    LLMService,
    LLMTechnicalError,
    _compute_backoff,
)

MINIMAL_PROFILE = {"profile": {"id": "minimal", "description": "test"}}


def _state() -> ConversationState:
    s = ConversationState()
    s.add("user", "oi")
    return s


class FailingBackend:
    provider_name = "stub"

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        raise self._error


class FailThenSucceedBackend:
    provider_name = "stub"

    def __init__(self, fail_times: int, error: Exception) -> None:
        self._fail_times = fail_times
        self._error = error
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return "ok", "stub-model", self.provider_name


# ── _compute_backoff unit tests ───────────────────────────────────────────────


def test_compute_backoff_retry1_equals_base(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    assert _compute_backoff(1, base_s=1.0, max_s=10.0) == pytest.approx(1.0)


def test_compute_backoff_retry2_doubles(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    assert _compute_backoff(2, base_s=1.0, max_s=10.0) == pytest.approx(2.0)


def test_compute_backoff_retry3_quadruples(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    assert _compute_backoff(3, base_s=1.0, max_s=10.0) == pytest.approx(4.0)


def test_compute_backoff_respects_cap(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    # 2^10 = 1024 >> max_s=10
    assert _compute_backoff(10, base_s=1.0, max_s=10.0) == pytest.approx(10.0)


def test_compute_backoff_cap_respected_with_positive_jitter(monkeypatch):
    """Cap deve ser aplicado APOS o jitter: resultado nunca excede max_s."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.2)  # jitter maximo
    # retry 10: raw_delay=512, jitter=512*0.2=102.4, total=614.4 → capped to 10.0
    result = _compute_backoff(10, base_s=1.0, max_s=10.0)
    assert result == pytest.approx(10.0)


def test_compute_backoff_positive_jitter_below_cap_not_truncated(monkeypatch):
    """Jitter positivo abaixo do cap nao deve ser truncado."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.2)  # jitter maximo
    # retry 1: raw_delay=1.0, jitter=0.2, total=1.2 → abaixo de max_s=10.0
    result = _compute_backoff(1, base_s=1.0, max_s=10.0)
    assert result == pytest.approx(1.2)


def test_compute_backoff_jitter_within_20_percent():
    """Sem mock: 1000 amostras devem estar em [0.8, 1.2] para retry 1."""
    for _ in range(1000):
        d = _compute_backoff(1, base_s=1.0, max_s=10.0)
        assert 0.8 <= d <= 1.2, f"Jitter fora de +/-20%: {d}"


def test_compute_backoff_zero_base_returns_zero(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    assert _compute_backoff(1, base_s=0.0, max_s=10.0) == pytest.approx(0.0)


def test_compute_backoff_never_negative(monkeypatch):
    """Com jitter negativo maximo e base pequena o resultado nunca e negativo."""
    monkeypatch.setattr("random.uniform", lambda a, b: -0.2)
    result = _compute_backoff(1, base_s=0.001, max_s=10.0)
    assert result >= 0.0


# ── LLMService sleeps with backoff ────────────────────────────────────────────


def test_service_sleeps_exponentially_on_unavailable_retries(monkeypatch):
    """LLMService deve dormir base*1 antes do retry 1, base*2 antes do retry 2."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    sleep_calls: list[float] = []

    with patch("assistente_voz.llm.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        backend = FailingBackend(ConnectionError("offline"))
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=2,
            backoff_base_s=1.0,
            backoff_max_s=10.0,
        )
        with pytest.raises(ConnectionError):
            svc.complete(_state())

    assert len(sleep_calls) == 2
    assert sleep_calls[0] == pytest.approx(1.0)  # retry 1: base * 2^0
    assert sleep_calls[1] == pytest.approx(2.0)  # retry 2: base * 2^1


def test_service_no_sleep_on_logical_error():
    """Erro logico nao deve gerar nenhum sleep."""
    import urllib.error

    sleep_calls: list[float] = []
    with patch("assistente_voz.llm.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        backend = FailingBackend(
            urllib.error.HTTPError(url="x", code=400, msg="bad", hdrs=None, fp=None)
        )
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=2,
            backoff_base_s=1.0,
            backoff_max_s=10.0,
        )
        with pytest.raises(urllib.error.HTTPError):
            svc.complete(_state())

    assert sleep_calls == []
    assert backend.calls == 1  # sem retry


def test_service_cap_respected(monkeypatch):
    """Nenhum sleep deve exceder backoff_max_s (sem considerar jitter)."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    sleep_calls: list[float] = []

    with patch("assistente_voz.llm.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        backend = FailingBackend(ConnectionError("offline"))
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=5,
            backoff_base_s=5.0,
            backoff_max_s=8.0,
        )
        with pytest.raises(ConnectionError):
            svc.complete(_state())

    assert all(s <= 8.0 for s in sleep_calls), f"Sleep excedeu cap: {sleep_calls}"


def test_service_succeeds_after_retry_with_correct_sleep(monkeypatch):
    """Apos retry bem-sucedido, sleep deve ter ocorrido uma vez."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    sleep_calls: list[float] = []

    with patch("assistente_voz.llm.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        backend = FailThenSucceedBackend(fail_times=1, error=ConnectionError("transient"))
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=2,
            backoff_base_s=2.0,
            backoff_max_s=10.0,
        )
        response = svc.complete(_state())

    assert response.text == "ok"
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(2.0)  # base * 2^0


# ── log content ───────────────────────────────────────────────────────────────


def test_log_contains_sleep_interval_on_retry(caplog, monkeypatch):
    """WARNING deve mencionar o intervalo de sleep em cada retry."""
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)

    with patch("assistente_voz.llm.time.sleep"):
        backend = FailingBackend(ConnectionError("offline"))
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=1,
            backoff_base_s=1.0,
            backoff_max_s=10.0,
        )
        with caplog.at_level(logging.WARNING, logger="assistente_voz.llm"):
            with pytest.raises(ConnectionError):
                svc.complete(_state())

    retry_logs = [m for m in caplog.messages if "retry in" in m]
    assert retry_logs, "WARNING deve mencionar 'retry in' com o intervalo"
    assert "1.00" in retry_logs[0]


def test_log_contains_error_code_on_each_attempt(caplog):
    """WARNING deve conter o codigo de erro estavel em cada tentativa."""
    with patch("assistente_voz.llm.time.sleep"):
        backend = FailingBackend(ConnectionError("offline"))
        svc = LLMService(
            backend=backend,
            profile_data=MINIMAL_PROFILE,
            max_retries=1,
            backoff_base_s=0.0,
        )
        with caplog.at_level(logging.WARNING, logger="assistente_voz.llm"):
            with pytest.raises(ConnectionError):
                svc.complete(_state())

    assert all(LLMErrorCode.UNAVAILABLE in m for m in caplog.messages if "LLM attempt" in m)


# ── from_env reads backoff vars ───────────────────────────────────────────────


def test_from_env_reads_backoff_base(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "0")
    monkeypatch.delenv("LLM_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_S", "3.5")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_MAX_S", "15.0")

    svc = LLMService.from_env(MINIMAL_PROFILE)

    assert svc._backoff_base_s == pytest.approx(3.5)
    assert svc._backoff_max_s == pytest.approx(15.0)


def test_from_env_backoff_defaults(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "0")
    monkeypatch.delenv("LLM_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_RETRY_BACKOFF_BASE_S", raising=False)
    monkeypatch.delenv("LLM_RETRY_BACKOFF_MAX_S", raising=False)

    svc = LLMService.from_env(MINIMAL_PROFILE)

    assert svc._backoff_base_s == pytest.approx(1.0)
    assert svc._backoff_max_s == pytest.approx(10.0)
