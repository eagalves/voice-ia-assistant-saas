"""Issue 27 – Streaming de Tokens do LLM na UI.

Garante que:
- LLMService.stream() acumula tokens e invoca on_token para cada um.
- time_to_first_token_ms é maior que zero quando ha tokens.
- Fallback para complete() quando o backend nao tem stream().
- Conteudo acumulado do streaming é igual ao texto final retornado.
- Backends sem stream() nao interrompem a execucao.
- LLMResponse tem campo time_to_first_token_ms.
"""
from __future__ import annotations

import threading
import time
from typing import Iterator

import pytest

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMResponse, LLMService


# ── perfil minimo ─────────────────────────────────────────────────────────────

MINIMAL_PROFILE = {"profile": {"id": "minimal", "description": "test"}}


# ── helpers ───────────────────────────────────────────────────────────────────


def _state() -> ConversationState:
    s = ConversationState()
    s.add("user", "Olá, como você está?")
    return s


# ── stubs de backend ──────────────────────────────────────────────────────────


class StreamingStubBackend:
    """Backend que suporta stream() e complete() com respostas fixas."""

    provider_name = "streaming-stub"

    def __init__(
        self,
        tokens: list[str],
        model: str = "stub-model",
        *,
        delay_s: float = 0.0,
    ) -> None:
        self._tokens = tokens
        self._model = model
        self._delay_s = delay_s
        self.complete_calls = 0
        self.stream_calls = 0

    def complete(self, messages: list[dict]) -> tuple[str, str, str]:
        self.complete_calls += 1
        return "".join(self._tokens), self._model, self.provider_name

    def stream(self, messages: list[dict]) -> Iterator[str]:
        self.stream_calls += 1
        for token in self._tokens:
            if self._delay_s > 0:
                time.sleep(self._delay_s)
            yield token


class NoStreamBackend:
    """Backend sem metodo stream() — apenas complete()."""

    provider_name = "no-stream-stub"

    def __init__(self, response: str = "resposta completa", model: str = "stub-model") -> None:
        self._response = response
        self._model = model
        self.complete_calls = 0

    def complete(self, messages: list[dict]) -> tuple[str, str, str]:
        self.complete_calls += 1
        return self._response, self._model, self.provider_name


class NotImplementedStreamBackend:
    """Backend com stream() que lanca NotImplementedError."""

    provider_name = "ni-stream-stub"

    def __init__(self, response: str = "resposta fallback", model: str = "stub-model") -> None:
        self._response = response
        self._model = model
        self.complete_calls = 0

    def complete(self, messages: list[dict]) -> tuple[str, str, str]:
        self.complete_calls += 1
        return self._response, self._model, self.provider_name

    def stream(self, messages: list[dict]) -> Iterator[str]:  # noqa: ARG002
        raise NotImplementedError("streaming not available")
        yield  # make it a generator


# ── LLMResponse tem time_to_first_token_ms ───────────────────────────────────


def test_llm_response_has_ttft_field():
    """LLMResponse deve ter campo time_to_first_token_ms com default 0.0."""
    r = LLMResponse(text="ok", model="m", latency_ms=100.0, provider="stub")
    assert hasattr(r, "time_to_first_token_ms")
    assert r.time_to_first_token_ms == 0.0


def test_llm_response_ttft_can_be_set():
    r = LLMResponse(text="ok", model="m", latency_ms=100.0, provider="stub", time_to_first_token_ms=42.5)
    assert r.time_to_first_token_ms == pytest.approx(42.5)


# ── LLMService.stream() — backend com streaming ──────────────────────────────


def test_stream_invokes_on_token_for_each_token():
    """on_token deve ser chamado uma vez por token."""
    tokens = ["Olá", ", ", "tudo", " bem", "?"]
    backend = StreamingStubBackend(tokens=tokens)
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    received: list[str] = []
    svc.stream(_state(), on_token=received.append)

    assert received == tokens


def test_stream_accumulated_text_equals_final_text():
    """Texto acumulado pelo chamador deve ser identico ao response.text."""
    tokens = ["res", "pos", "ta", " aqui"]
    backend = StreamingStubBackend(tokens=tokens)
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    received: list[str] = []
    response = svc.stream(_state(), on_token=received.append)

    assert "".join(received) == response.text
    assert response.text == "resposta aqui"


def test_stream_returns_llm_response():
    """stream() deve retornar um LLMResponse."""
    backend = StreamingStubBackend(tokens=["tok"])
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    response = svc.stream(_state(), on_token=lambda t: None)
    assert isinstance(response, LLMResponse)


def test_stream_ttft_greater_than_zero_when_tokens_exist():
    """time_to_first_token_ms deve ser > 0 quando ha pelo menos um token."""
    backend = StreamingStubBackend(tokens=["primeiro", " token"])
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    response = svc.stream(_state(), on_token=lambda t: None)
    assert response.time_to_first_token_ms > 0.0


def test_stream_ttft_less_than_total_latency():
    """TTFT deve ser menor que a latencia total (medida do primeiro token, nao do ultimo)."""
    # Tokens com pequeno delay para garantir diferenca mensuravel.
    backend = StreamingStubBackend(tokens=["a", "b", "c", "d", "e"], delay_s=0.01)
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    response = svc.stream(_state(), on_token=lambda t: None)
    # TTFT deve ser menor que latency_ms total (que inclui todos os tokens).
    assert response.time_to_first_token_ms < response.latency_ms


def test_stream_provider_name_propagated():
    """provider do response deve ser o provider_name do backend."""
    backend = StreamingStubBackend(tokens=["ok"])
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    response = svc.stream(_state(), on_token=lambda t: None)
    assert response.provider == "streaming-stub"


# ── testes dos bugs corrigidos (revisao pos-implementacao) ────────────────────


class _RendererCapture:
    """Renderer fake que captura todas as chamadas para verificacao em testes."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.replies: list[str] = []
        self.streaming_ends: list[dict] = []
        self.errors: list[Exception] = []
        self._streaming_dirty = False  # espelha o flag do renderer real

    def assistant_token(self, token: str) -> None:
        self.tokens.append(token)
        self._streaming_dirty = True

    def assistant_reply_end(self, model: str, latency_ms: float, ttft_ms: float = 0.0) -> None:
        self.streaming_ends.append({"model": model, "ttft_ms": ttft_ms})
        self._streaming_dirty = False

    def assistant_reply(self, text: str, model: str, latency_ms: float) -> None:
        self.replies.append(text)

    def assistant_error(self, exc: Exception) -> None:
        self.errors.append(exc)


class _NoStreamFakeLLM:
    """LLMService fake sem stream() — cai no fallback via AttributeError."""

    def stream(self, state, on_token):
        raise AttributeError("no stream support")

    def complete(self, state):
        return LLMResponse(text="fallback completo", model="test", latency_ms=1.0, provider="test")


class _MidStreamFailFakeLLM:
    """LLMService fake que emite um token e depois falha com ConnectionError."""

    def stream(self, state, on_token):
        on_token("olá")
        raise ConnectionError("rede caiu")

    def complete(self, state):
        return LLMResponse(text="ok", model="test", latency_ms=1.0, provider="test")


def _make_llm_worker(fake_llm):
    from assistente_voz.conversation_state import ConversationState
    from assistente_voz.voice_cli import LLMWorker

    state = ConversationState()
    renderer = _RendererCapture()
    worker = LLMWorker(llm=fake_llm, state=state, renderer=renderer)
    return worker, state, renderer


# ── fix 1: sem duplicacao do user quando AttributeError aciona fallback ───────


def test_no_user_duplication_when_stream_raises_attribute_error():
    """Quando stream() levanta AttributeError, o user nao deve ser adicionado duas vezes."""
    worker, state, renderer = _make_llm_worker(_NoStreamFakeLLM())
    worker.start()
    worker.enqueue("minha pergunta")
    worker.shutdown(per_call_timeout_s=5.0)

    msgs = state.to_llm_messages()
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 1, (
        f"Esperado 1 mensagem user, obtido {len(user_msgs)}: {user_msgs}"
    )


def test_assistant_message_added_after_attribute_error_fallback():
    """Apos fallback por AttributeError, resposta do assistant deve estar no estado."""
    worker, state, renderer = _make_llm_worker(_NoStreamFakeLLM())
    worker.start()
    worker.enqueue("pergunta")
    worker.shutdown(per_call_timeout_s=5.0)

    msgs = state.to_llm_messages()
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "fallback completo"


# ── fix 2: linha de streaming fechada quando stream falha apos primeiro token ──


def test_streaming_dirty_flag_cleared_by_reply_end():
    """assistant_reply_end deve limpar o flag de streaming aberto."""
    renderer = _RendererCapture()
    renderer.assistant_token("abc")
    assert renderer._streaming_dirty is True
    renderer.assistant_reply_end(model="m", latency_ms=1.0, ttft_ms=10.0)
    assert renderer._streaming_dirty is False


def test_streaming_dirty_flag_cleared_by_error(capsys):
    """CompletudeRenderer.assistant_error deve fechar linha de streaming aberta."""
    import time
    from assistente_voz.voice_cli import CompletudeRenderer

    renderer = CompletudeRenderer(started_at=time.time())
    renderer.assistant_token("parcial")
    assert renderer._streaming_dirty is True

    renderer.assistant_error(ConnectionError("falhou"))

    assert renderer._streaming_dirty is False
    out = capsys.readouterr().out
    # A linha de erro deve comecar apos um newline (fechando a linha de tokens)
    assert "\n[assistant-error]" in out


# ── fix 3: estado nao fica corrompido quando stream falha apos primeiro token ──


def test_state_has_assistant_placeholder_after_mid_stream_failure():
    """Quando stream falha apos emitir tokens, estado deve ter mensagem assistant."""
    worker, state, renderer = _make_llm_worker(_MidStreamFailFakeLLM())
    worker.start()
    worker.enqueue("oi")
    worker.shutdown(per_call_timeout_s=5.0)

    msgs = state.to_llm_messages()
    roles = [m["role"] for m in msgs]
    assert "assistant" in roles, (
        "Estado deve ter mensagem assistant mesmo apos falha no stream"
    )
    assert roles[-1] == "assistant", (
        "Ultima mensagem deve ser assistant (nao user) para evitar dois users consecutivos"
    )
    assert len(renderer.errors) == 1
    assert isinstance(renderer.errors[0], ConnectionError)


def test_token_visible_before_mid_stream_failure():
    """Token emitido antes da falha deve ter chegado ao renderer."""
    worker, state, renderer = _make_llm_worker(_MidStreamFailFakeLLM())
    worker.start()
    worker.enqueue("oi")
    worker.shutdown(per_call_timeout_s=5.0)

    assert "olá" in renderer.tokens


def test_second_utterance_succeeds_after_mid_stream_failure():
    """Segundo utterance deve funcionar normalmente apos falha de stream no primeiro.

    Garante que o placeholder de assistant evita dois user consecutivos e que
    a proxima chamada ao LLM recebe um historico valido.
    """
    call_count = 0

    class _FailFirstThenOkLLM:
        def stream(self, state, on_token):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                on_token("parcial")
                raise ConnectionError("falha no primeiro")
            on_token("segunda ")
            on_token("resposta")
            from assistente_voz.llm import LLMResponse
            return LLMResponse(text="segunda resposta", model="test", latency_ms=1.0,
                               provider="test", time_to_first_token_ms=0.5)

        def complete(self, state):
            from assistente_voz.llm import LLMResponse
            return LLMResponse(text="ok", model="test", latency_ms=1.0, provider="test")

    worker, state, renderer = _make_llm_worker(_FailFirstThenOkLLM())
    worker.start()
    worker.enqueue("primeiro")
    worker.enqueue("segundo")
    worker.shutdown(per_call_timeout_s=5.0)

    msgs = state.to_llm_messages()
    roles = [m["role"] for m in msgs]

    # Nunca deve haver dois user consecutivos
    for i in range(len(roles) - 1):
        assert not (roles[i] == "user" and roles[i + 1] == "user"), (
            f"Dois user consecutivos detectados em posicoes {i} e {i+1}: {roles}"
        )

    # Segundo utterance deve ter produzido uma resposta ou ao menos estar no estado
    assert roles.count("user") == 2
    assert len(renderer.errors) == 1
    assert isinstance(renderer.errors[0], ConnectionError)


def test_assistant_reply_closes_streaming_dirty_when_ttft_rounds_to_zero(capsys):
    """assistant_reply() deve fechar linha de streaming mesmo quando ttft_ms == 0."""
    import time
    from assistente_voz.voice_cli import CompletudeRenderer

    renderer = CompletudeRenderer(started_at=time.time())
    renderer.assistant_token("token")          # abre linha de streaming
    assert renderer._streaming_dirty is True

    renderer.assistant_reply("texto completo", "model", 100.0)  # deveria fechar

    assert renderer._streaming_dirty is False
    out = capsys.readouterr().out
    # Linha do token + \n de fechamento + linha de assistant_reply
    assert "\n[assistant model=" in out


def test_stream_uses_stream_not_complete_when_available():
    """LLMService.stream() deve chamar stream(), nao complete(), quando disponivel."""
    backend = StreamingStubBackend(tokens=["tok1", "tok2"])
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    svc.stream(_state(), on_token=lambda t: None)
    assert backend.stream_calls == 1
    assert backend.complete_calls == 0


# ── fallback para complete() quando backend nao tem stream() ─────────────────


def test_stream_falls_back_to_complete_when_no_stream_method():
    """Quando o backend nao tem stream(), deve cair em complete() sem erro."""
    backend = NoStreamBackend(response="resposta completa")
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    received: list[str] = []
    response = svc.stream(_state(), on_token=received.append)

    # complete() foi chamado
    assert backend.complete_calls == 1
    # Texto correto
    assert response.text == "resposta completa"
    # on_token NAO é chamado no fallback (comportamento de complete)
    assert received == []


def test_stream_fallback_ttft_is_zero():
    """Quando cai em complete(), time_to_first_token_ms deve ser 0.0."""
    backend = NoStreamBackend(response="ok")
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    response = svc.stream(_state(), on_token=lambda t: None)
    assert response.time_to_first_token_ms == 0.0


def test_stream_falls_back_when_stream_raises_not_implemented():
    """Quando stream() lanca NotImplementedError, deve usar complete()."""
    backend = NotImplementedStreamBackend(response="fallback ok")
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    response = svc.stream(_state(), on_token=lambda t: None)

    assert response.text == "fallback ok"
    assert backend.complete_calls == 1


# ── conteudo do streaming identico ao complete() ────────────────────────────


def test_streaming_content_matches_complete_content():
    """O texto acumulado via streaming deve ser identico ao retornado por complete()."""
    tokens = ["Esta", " é", " a", " resposta", " completa."]
    backend = StreamingStubBackend(tokens=tokens)
    svc_stream = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)
    svc_complete = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    state_s = _state()
    state_c = _state()

    received: list[str] = []
    stream_response = svc_stream.stream(state_s, on_token=received.append)
    complete_response = svc_complete.complete(state_c)

    assert "".join(received) == complete_response.text
    assert stream_response.text == complete_response.text


# ── primeiro token aparece antes da resposta completa ────────────────────────


def test_first_token_arrives_before_stream_completes():
    """Primeiro token deve ser recebido enquanto outros ainda estao sendo gerados."""
    first_token_time: list[float] = []
    all_tokens_done_time: list[float] = []

    # Tokens com delay para simular geracao lenta
    tokens = ["token1", "token2", "token3"]

    class SlowStreamBackend:
        provider_name = "slow-stub"
        _model = "slow-model"

        def complete(self, messages):
            return "".join(tokens), "slow-model", self.provider_name

        def stream(self, messages):
            for i, tok in enumerate(tokens):
                if i > 0:
                    time.sleep(0.05)  # delay entre tokens
                yield tok

    backend = SlowStreamBackend()
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    def on_token(t: str) -> None:
        if not first_token_time:
            first_token_time.append(time.perf_counter())

    response = svc.stream(_state(), on_token=on_token)
    all_tokens_done_time.append(time.perf_counter())

    assert first_token_time, "Nenhum token recebido"
    # O primeiro token chegou antes de todos os tokens terminarem
    assert first_token_time[0] <= all_tokens_done_time[0]
    # TTFT deve refletir chegada do primeiro token
    assert response.time_to_first_token_ms > 0


# ── empty stream ──────────────────────────────────────────────────────────────


def test_stream_with_no_tokens_returns_empty_text():
    """Stream sem tokens deve retornar texto vazio e ttft = 0."""
    backend = StreamingStubBackend(tokens=[])
    svc = LLMService(backend=backend, profile_data=MINIMAL_PROFILE)

    received: list[str] = []
    response = svc.stream(_state(), on_token=received.append)

    assert response.text == ""
    assert received == []
    assert response.time_to_first_token_ms == 0.0
