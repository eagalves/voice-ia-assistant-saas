"""Issue 20 – Encerramento Limpo do Worker de LLM no Voice CLI.

Garante que:
- shutdown() aguarda chamada LLM em andamento e retorna True (sem warning).
- shutdown() drena todos os itens da fila antes de encerrar.
- shutdown() retorna False apenas quando o timeout e genuinamente excedido.
- enqueue() descarta o item mais antigo quando a fila esta cheia.
- Nenhuma thread fica pendurada apos encerramento limpo.
"""
from __future__ import annotations

import threading
import time

from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMResponse
from assistente_voz.voice_cli import LLMWorker


# ── fake renderer ─────────────────────────────────────────────────────────────


class _FakeRenderer:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.errors: list[Exception] = []
        self.tokens: list[str] = []
        self.streaming_ends: list[dict] = []

    def assistant_reply(self, text: str, model: str, latency_ms: float) -> None:
        self.replies.append(text)

    def assistant_reply_end(self, model: str, latency_ms: float, ttft_ms: float = 0.0) -> None:
        self.streaming_ends.append({"model": model, "latency_ms": latency_ms, "ttft_ms": ttft_ms})

    def assistant_token(self, token: str) -> None:
        self.tokens.append(token)

    def assistant_error(self, exc: Exception) -> None:
        self.errors.append(exc)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_fake_llm(query_fn):
    """Wrap a (text, llm, state) → (reply, model, latency_ms) function into a fake LLM.

    stream() raises AttributeError so _run() falls through to complete().
    complete() extracts the last user message from state and delegates to query_fn.
    """
    class _FakeLLM:
        def stream(self, state, on_token):
            raise AttributeError("test fake: no stream support")

        def complete(self, state):
            msgs = state.to_llm_messages()
            text = next(
                (m["content"] for m in reversed(msgs) if m["role"] == "user"), ""
            )
            reply, model, latency_ms = query_fn(text, self, state)
            return LLMResponse(text=reply, model=model, latency_ms=latency_ms, provider=model)

    return _FakeLLM()


def _make_worker(query_fn) -> tuple[LLMWorker, _FakeRenderer]:
    """Build a LLMWorker backed by query_fn with a real ConversationState."""
    renderer = _FakeRenderer()
    worker = LLMWorker(
        llm=_make_fake_llm(query_fn),
        state=ConversationState(),
        renderer=renderer,
    )
    return worker, renderer


# ── encerramento limpo com chamada em andamento ───────────────────────────────


def test_shutdown_waits_for_slow_call_in_progress():
    """Worker com chamada lenta em andamento deve aguardar e sair limpo."""
    call_started = threading.Event()
    allow_finish = threading.Event()

    def slow_query(text, llm, state):
        call_started.set()
        allow_finish.wait(timeout=5)
        return "ok", "test-model", 200.0

    worker, renderer = _make_worker(slow_query)
    worker.start()
    worker.enqueue("ola")

    call_started.wait(timeout=2)
    allow_finish.set()

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean, "shutdown() deve retornar True quando a chamada concluiu a tempo"
    assert not worker._thread.is_alive(), "Thread nao deve ficar pendurada"
    assert renderer.replies == ["ok"]


def test_shutdown_returns_true_even_if_call_was_still_running_when_shutdown_called():
    """Shutdown iniciado durante chamada em andamento deve aguardar conclusao."""
    call_started = threading.Event()
    call_done = threading.Event()

    def slow_query(text, llm, state):
        call_started.set()
        time.sleep(0.3)
        call_done.set()
        return "resposta", "model", 300.0

    worker, renderer = _make_worker(slow_query)
    worker.start()
    worker.enqueue("pergunta")

    call_started.wait(timeout=2)
    clean = worker.shutdown(per_call_timeout_s=2.0)

    assert clean
    assert call_done.is_set(), "Chamada deve ter concluido antes do shutdown retornar"
    assert renderer.replies == ["resposta"]


# ── drenagem da fila ──────────────────────────────────────────────────────────


def test_shutdown_drains_all_queued_items():
    """Todos os itens enfileirados devem ser processados antes do encerramento."""
    processed: list[str] = []

    def instant_query(text, llm, state):
        processed.append(text)
        return f"resp:{text}", "model", 1.0

    worker, renderer = _make_worker(instant_query)
    worker.start()

    for i in range(5):
        worker.enqueue(f"item-{i}")

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean
    assert len(processed) == 5
    assert len(renderer.replies) == 5


def test_shutdown_timeout_covers_multiple_queued_items():
    """Timeout deve escalar com o numero de itens na fila."""
    call_count = 0

    def slow_query(text, llm, state):
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)
        return "ok", "model", 100.0

    worker, renderer = _make_worker(slow_query)
    worker.start()

    n_items = 4
    for i in range(n_items):
        worker.enqueue(f"item-{i}")

    clean = worker.shutdown(per_call_timeout_s=1.0)

    assert clean
    assert call_count == n_items


# ── timeout genuino ───────────────────────────────────────────────────────────


def test_shutdown_returns_false_on_genuine_timeout(monkeypatch):
    """Quando o timeout e genuinamente excedido, shutdown() deve retornar False."""
    monkeypatch.setattr(LLMWorker, "_SHUTDOWN_OVERHEAD_S", 0.0)

    call_started = threading.Event()
    unblock = threading.Event()

    def blocking_query(text, llm, state):
        call_started.set()
        unblock.wait(timeout=10)
        return "never", "model", 0.0

    worker, _ = _make_worker(blocking_query)
    worker.start()
    worker.enqueue("trava")

    call_started.wait(timeout=2)

    clean = worker.shutdown(per_call_timeout_s=0.1)

    unblock.set()
    worker._thread.join(timeout=5)

    assert clean is False, "shutdown() deve retornar False quando o timeout expira"
    assert not worker._thread.is_alive()


def test_thread_not_alive_after_clean_shutdown():
    """Apos shutdown limpo, a thread nao deve estar viva."""
    def instant_query(text, llm, state):
        return "ok", "model", 1.0

    worker, _ = _make_worker(instant_query)
    worker.start()
    worker.enqueue("teste")
    worker.shutdown(per_call_timeout_s=5.0)

    assert not worker._thread.is_alive()


# ── enqueue com fila cheia ────────────────────────────────────────────────────


def test_enqueue_drops_oldest_when_full():
    """Quando a fila esta cheia, enqueue descarta o item mais antigo."""
    worker = LLMWorker(llm=None, state=None, renderer=_FakeRenderer(), queue_maxsize=2)

    worker.enqueue("primeiro")
    worker.enqueue("segundo")
    worker.enqueue("terceiro")  # overflow: descarta "primeiro"

    items = []
    while not worker._queue.empty():
        items.append(worker._queue.get_nowait())

    assert items == ["segundo", "terceiro"], (
        f"Esperado ['segundo', 'terceiro'], obtido {items}"
    )


def test_enqueue_noop_after_shutdown():
    """enqueue() nao deve aceitar itens apos shutdown() ser chamado."""
    worker = LLMWorker(llm=None, state=None, renderer=_FakeRenderer(), queue_maxsize=10)

    worker.enqueue("antes")
    worker._stop.set()
    worker.enqueue("depois")

    items = []
    while not worker._queue.empty():
        items.append(worker._queue.get_nowait())

    assert items == ["antes"], f"Item apos stop nao deve ser enfileirado: {items}"


# ── erro no LLM nao trava o worker ───────────────────────────────────────────


def test_worker_continues_after_llm_error():
    """Erro em uma chamada LLM nao deve travar o worker — proximo item e processado."""
    call_results: list[str] = []

    def query_with_error(text, llm, state):
        if text == "erro":
            raise RuntimeError("LLM falhou")
        call_results.append(text)
        return f"resp:{text}", "model", 1.0

    worker, renderer = _make_worker(query_with_error)
    worker.start()
    worker.enqueue("erro")
    worker.enqueue("ok")

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean
    assert "ok" in call_results
    assert len(renderer.errors) == 1
    assert isinstance(renderer.errors[0], RuntimeError)
