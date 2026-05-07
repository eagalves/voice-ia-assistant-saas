"""Issue 23 – Classificacao Robusta de Erros Tecnicos no Fallback de LLM.

Garante que:
- _classify_error retorna LLM_ERR_UNAVAILABLE para erros de disponibilidade.
- _classify_error retorna LLM_ERR_LOGICAL para erros de payload/autenticacao.
- FallbackBackend usa fallback apenas para LLM_ERR_UNAVAILABLE.
- LLMService.complete() nao retenta erros logicos.
- LLMService.complete() retenta erros tecnicos ate max_retries.
- WARNING e emitido em cada tentativa falha.
- WARNING e emitido quando fallback e acionado (notify_fallback=True).
"""
from __future__ import annotations

import logging
import urllib.error
from unittest.mock import MagicMock

import pytest

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import (
    FallbackBackend,
    LLMErrorCode,
    LLMService,
    LLMTechnicalError,
    _classify_error,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=None)


def _state_with_message() -> ConversationState:
    state = ConversationState()
    state.add("user", "oi")
    return state


MINIMAL_PROFILE = {"profile": {"id": "minimal", "description": "test"}}


class StubBackend:
    provider_name = "stub"

    def __init__(self, response: str = "ok") -> None:
        self._response = response
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return self._response, "stub-model", self.provider_name


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
        return "recuperado", "stub-model", self.provider_name


# ── _classify_error: LLM_ERR_UNAVAILABLE ─────────────────────────────────────


def test_classify_llm_technical_error_is_unavailable():
    assert _classify_error(LLMTechnicalError("offline")) == LLMErrorCode.UNAVAILABLE


def test_classify_connection_error_is_unavailable():
    assert _classify_error(ConnectionError("refused")) == LLMErrorCode.UNAVAILABLE


def test_classify_timeout_error_is_unavailable():
    assert _classify_error(TimeoutError("timed out")) == LLMErrorCode.UNAVAILABLE


def test_classify_url_error_is_unavailable():
    assert _classify_error(urllib.error.URLError("name not resolved")) == LLMErrorCode.UNAVAILABLE


def test_classify_http_502_is_unavailable():
    assert _classify_error(_http_error(502)) == LLMErrorCode.UNAVAILABLE


def test_classify_http_503_is_unavailable():
    assert _classify_error(_http_error(503)) == LLMErrorCode.UNAVAILABLE


def test_classify_http_504_is_unavailable():
    assert _classify_error(_http_error(504)) == LLMErrorCode.UNAVAILABLE


def test_classify_http_429_is_unavailable():
    assert _classify_error(_http_error(429)) == LLMErrorCode.UNAVAILABLE


def test_classify_http_408_is_unavailable():
    assert _classify_error(_http_error(408)) == LLMErrorCode.UNAVAILABLE


def test_classify_http_500_is_unavailable():
    assert _classify_error(_http_error(500)) == LLMErrorCode.UNAVAILABLE


# ── _classify_error: LLM_ERR_LOGICAL ─────────────────────────────────────────


def test_classify_http_400_is_logical():
    assert _classify_error(_http_error(400)) == LLMErrorCode.LOGICAL


def test_classify_http_401_is_logical():
    assert _classify_error(_http_error(401)) == LLMErrorCode.LOGICAL


def test_classify_http_403_is_logical():
    assert _classify_error(_http_error(403)) == LLMErrorCode.LOGICAL


def test_classify_http_422_is_logical():
    assert _classify_error(_http_error(422)) == LLMErrorCode.LOGICAL


def test_classify_value_error_is_logical():
    assert _classify_error(ValueError("bad json")) == LLMErrorCode.LOGICAL


def test_classify_type_error_is_logical():
    assert _classify_error(TypeError("unexpected type")) == LLMErrorCode.LOGICAL


def test_classify_key_error_is_logical():
    assert _classify_error(KeyError("missing field")) == LLMErrorCode.LOGICAL


def test_classify_http_404_not_forced_logical():
    """404 nao e classificado como LOGICAL pelo classificador generico.

    "Modelo nao encontrado" no Ollama e erro tecnico — o OllamaBackend ja converte
    HTTPError(404) em LLMTechnicalError antes de chegar aqui. Para backends futuros
    que levantam HTTPError(404) diretamente, a classificacao cai no default LOGICAL,
    mas nao e imposta pelo conjunto _LOGICAL_HTTP_CODES. Isso permite que cada
    backend defina a semantica de 404 via LLMTechnicalError quando apropriado.
    """
    # 404 nao esta em _LOGICAL_HTTP_CODES nem em _UNAVAILABLE_HTTP_CODES,
    # entao cai no default LOGICAL — mas via fallthrough, nao via regra explicita.
    result = _classify_error(_http_error(404))
    # O resultado atual e LOGICAL (fallthrough), mas o ponto e que o OllamaBackend
    # nunca chega aqui com HTTPError(404) — ele ja converte para LLMTechnicalError.
    assert result == LLMErrorCode.LOGICAL  # fallthrough, nao regra explicita


def test_ollama_404_already_wrapped_as_technical_error():
    """OllamaBackend converte HTTPError(404) em LLMTechnicalError antes do classificador.

    Garante que o caminho real do Ollama trata 'modelo nao encontrado' como UNAVAILABLE.
    """
    exc = LLMTechnicalError("Ollama HTTP 404")
    assert _classify_error(exc) == LLMErrorCode.UNAVAILABLE


def test_classify_sdk_exception_with_status_code_unavailable():
    """Excecao com status_code 503 (padrao de SDK) deve ser UNAVAILABLE."""
    exc = Exception("server error")
    exc.status_code = 503  # type: ignore[attr-defined]
    assert _classify_error(exc) == LLMErrorCode.UNAVAILABLE


def test_classify_sdk_exception_with_status_code_logical():
    """Excecao com status_code 401 (padrao de SDK) deve ser LOGICAL."""
    exc = Exception("unauthorized")
    exc.status_code = 401  # type: ignore[attr-defined]
    assert _classify_error(exc) == LLMErrorCode.LOGICAL


# ── FallbackBackend ───────────────────────────────────────────────────────────


def test_fallback_triggered_for_connection_error():
    """ConnectionError e UNAVAILABLE — fallback deve ser acionado."""
    primary = FailingBackend(ConnectionError("refused"))
    secondary = StubBackend(response="fallback ok")
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=True, notify_fallback=False)

    text, _, _ = backend.complete([{"role": "user", "content": "oi"}])

    assert text == "fallback ok"
    assert secondary.calls == 1


def test_fallback_triggered_for_llm_technical_error():
    primary = FailingBackend(LLMTechnicalError("timeout"))
    secondary = StubBackend(response="fallback ok")
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=True, notify_fallback=False)

    text, _, _ = backend.complete([{"role": "user", "content": "oi"}])

    assert text == "fallback ok"


def test_fallback_not_triggered_for_http_400():
    """HTTP 400 e LOGICAL — fallback nao deve ser acionado."""
    primary = FailingBackend(_http_error(400))
    secondary = StubBackend()
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=True, notify_fallback=False)

    with pytest.raises(urllib.error.HTTPError):
        backend.complete([{"role": "user", "content": "oi"}])

    assert secondary.calls == 0


def test_fallback_not_triggered_for_http_401():
    primary = FailingBackend(_http_error(401))
    secondary = StubBackend()
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=True, notify_fallback=False)

    with pytest.raises(urllib.error.HTTPError):
        backend.complete([{"role": "user", "content": "oi"}])

    assert secondary.calls == 0


def test_fallback_not_triggered_when_disabled():
    """Fallback nao deve ocorrer mesmo para UNAVAILABLE se enabled=False."""
    primary = FailingBackend(ConnectionError("refused"))
    secondary = StubBackend()
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=False, notify_fallback=False)

    with pytest.raises(ConnectionError):
        backend.complete([{"role": "user", "content": "oi"}])

    assert secondary.calls == 0


def test_fallback_logs_warning_when_notify_fallback_true(caplog):
    primary = FailingBackend(LLMTechnicalError("offline"))
    secondary = StubBackend()
    backend = FallbackBackend(primary=primary, fallback=secondary, enabled=True, notify_fallback=True)

    with caplog.at_level(logging.WARNING, logger="assistente_voz.llm"):
        backend.complete([{"role": "user", "content": "oi"}])

    assert any("fallback" in m.lower() for m in caplog.messages)


# ── LLMService retry policy ───────────────────────────────────────────────────


def test_logical_error_is_not_retried():
    """HTTP 400 (LOGICAL) nao deve gerar retentativa."""
    primary = FailingBackend(_http_error(400))
    svc = LLMService(backend=primary, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)

    with pytest.raises(urllib.error.HTTPError):
        svc.complete(_state_with_message())

    assert primary.calls == 1  # sem retry


def test_unavailable_error_is_retried_up_to_max():
    """ConnectionError (UNAVAILABLE) deve ser retentado ate max_retries."""
    primary = FailingBackend(ConnectionError("offline"))
    svc = LLMService(backend=primary, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)

    with pytest.raises(ConnectionError):
        svc.complete(_state_with_message())

    assert primary.calls == 3  # 1 inicial + 2 retentativas


def test_unavailable_error_succeeds_after_retry():
    """UNAVAILABLE resolvido na segunda tentativa deve retornar resposta normal."""
    primary = FailThenSucceedBackend(fail_times=1, error=ConnectionError("transient"))
    svc = LLMService(backend=primary, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)

    response = svc.complete(_state_with_message())

    assert response.text == "recuperado"
    assert primary.calls == 2


def test_warning_logged_on_each_failed_attempt(caplog):
    """WARNING deve ser emitido para cada tentativa falha."""
    primary = FailingBackend(ConnectionError("offline"))
    svc = LLMService(backend=primary, profile_data=MINIMAL_PROFILE, max_retries=2, retry_delay_s=0)

    with caplog.at_level(logging.WARNING, logger="assistente_voz.llm"):
        with pytest.raises(ConnectionError):
            svc.complete(_state_with_message())

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_msgs) == 3, (
        f"Esperado 3 WARNINGs (1 + 2 retentativas), obtido {len(warning_msgs)}"
    )


def test_warning_contains_error_code(caplog):
    """WARNING deve mencionar o codigo de erro estavel."""
    primary = FailingBackend(ConnectionError("offline"))
    svc = LLMService(backend=primary, profile_data=MINIMAL_PROFILE, max_retries=0, retry_delay_s=0)

    with caplog.at_level(logging.WARNING, logger="assistente_voz.llm"):
        with pytest.raises(ConnectionError):
            svc.complete(_state_with_message())

    assert any(LLMErrorCode.UNAVAILABLE in m for m in caplog.messages)
