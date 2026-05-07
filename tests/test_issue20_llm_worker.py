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

from assistente_voz.voice_cli import LLMWorker


# ── fake renderer ─────────────────────────────────────────────────────────────


class _FakeRenderer:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.errors: list[Exception] = []

    def assistant_reply(self, text: str, model: str, latency_ms: float) -> None:
        self.replies.append(text)

    def assistant_error(self, exc: Exception) -> None:
        self.errors.append(exc)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_worker(monkeypatch, query_fn) -> tuple[LLMWorker, _FakeRenderer]:
    """Build a worker with a monkeypatched _query_llm_once."""
    monkeypatch.setattr("assistente_voz.voice_cli._query_llm_once", query_fn)
    renderer = _FakeRenderer()
    worker = LLMWorker(llm=None, state=None, renderer=renderer)
    return worker, renderer


# ── encerramento limpo com chamada em andamento ───────────────────────────────


def test_shutdown_waits_for_slow_call_in_progress(monkeypatch):
    """Worker com chamada lenta em andamento deve aguardar e sair limpo."""
    call_started = threading.Event()
    allow_finish = threading.Event()

    def slow_query(text, llm, state):
        call_started.set()
        allow_finish.wait(timeout=5)
        return "ok", "test-model", 200.0

    worker, renderer = _make_worker(monkeypatch, slow_query)
    worker.start()
    worker.enqueue("ola")

    call_started.wait(timeout=2)  # aguarda a chamada comecar
    allow_finish.set()            # libera a chamada antes do shutdown

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean, "shutdown() deve retornar True quando a chamada concluiu a tempo"
    assert not worker._thread.is_alive(), "Thread nao deve ficar pendurada"
    assert renderer.replies == ["ok"]


def test_shutdown_returns_true_even_if_call_was_still_running_when_shutdown_called(monkeypatch):
    """Shutdown iniciado durante chamada em andamento deve aguardar conclusao."""
    call_started = threading.Event()
    call_done = threading.Event()

    def slow_query(text, llm, state):
        call_started.set()
        time.sleep(0.3)  # simula Ollama lento
        call_done.set()
        return "resposta", "model", 300.0

    worker, renderer = _make_worker(monkeypatch, slow_query)
    worker.start()
    worker.enqueue("pergunta")

    call_started.wait(timeout=2)
    # shutdown chamado ENQUANTO a chamada esta em andamento
    clean = worker.shutdown(per_call_timeout_s=2.0)

    assert clean
    assert call_done.is_set(), "Chamada deve ter concluido antes do shutdown retornar"
    assert renderer.replies == ["resposta"]


# ── drenagem da fila ──────────────────────────────────────────────────────────


def test_shutdown_drains_all_queued_items(monkeypatch):
    """Todos os itens enfileirados devem ser processados antes do encerramento."""
    processed: list[str] = []

    def instant_query(text, llm, state):
        processed.append(text)
        return f"resp:{text}", "model", 1.0

    worker, renderer = _make_worker(monkeypatch, instant_query)
    worker.start()

    for i in range(5):
        worker.enqueue(f"item-{i}")

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean
    assert len(processed) == 5
    assert len(renderer.replies) == 5


def test_shutdown_timeout_covers_multiple_queued_items(monkeypatch):
    """Timeout deve escalar com o numero de itens na fila."""
    call_count = 0

    def slow_query(text, llm, state):
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)  # cada item leva 0.1s
        return "ok", "model", 100.0

    worker, renderer = _make_worker(monkeypatch, slow_query)
    worker.start()

    n_items = 4
    for i in range(n_items):
        worker.enqueue(f"item-{i}")

    # per_call_timeout=1s, 4 itens → timeout = 1*(4+1)+5 = 10s (muito mais que 4*0.1=0.4s)
    clean = worker.shutdown(per_call_timeout_s=1.0)

    assert clean
    assert call_count == n_items


# ── timeout genuino ───────────────────────────────────────────────────────────


def test_shutdown_returns_false_on_genuine_timeout(monkeypatch):
    """Quando o timeout e genuinamente excedido, shutdown() deve retornar False.

    Monkeypatcha _SHUTDOWN_OVERHEAD_S=0 para que o timeout total seja apenas
    per_call_timeout_s (0.1s), mantendo o teste rapido sem espera real de 5s.
    """
    monkeypatch.setattr(LLMWorker, "_SHUTDOWN_OVERHEAD_S", 0.0)

    call_started = threading.Event()
    unblock = threading.Event()

    def blocking_query(text, llm, state):
        call_started.set()
        unblock.wait(timeout=10)
        return "never", "model", 0.0

    worker, _ = _make_worker(monkeypatch, blocking_query)
    worker.start()
    worker.enqueue("trava")

    call_started.wait(timeout=2)

    # timeout = 0.1 * (0+1) + 0 = 0.1s — expira antes da chamada bloqueante terminar
    clean = worker.shutdown(per_call_timeout_s=0.1)

    unblock.set()
    worker._thread.join(timeout=5)

    assert clean is False, "shutdown() deve retornar False quando o timeout expira"
    assert not worker._thread.is_alive()


def test_thread_not_alive_after_clean_shutdown(monkeypatch):
    """Apos shutdown limpo, a thread nao deve estar viva."""
    def instant_query(text, llm, state):
        return "ok", "model", 1.0

    worker, _ = _make_worker(monkeypatch, instant_query)
    worker.start()
    worker.enqueue("teste")
    worker.shutdown(per_call_timeout_s=5.0)

    assert not worker._thread.is_alive()


# ── enqueue com fila cheia ────────────────────────────────────────────────────


def test_enqueue_drops_oldest_when_full():
    """Quando a fila esta cheia, enqueue descarta o item mais antigo.

    Testa enqueue() diretamente sem iniciar a thread — evita racing com o worker
    e torna o comportamento completamente deterministico.
    """
    worker = LLMWorker(llm=None, state=None, renderer=_FakeRenderer(), queue_maxsize=2)
    # thread NAO iniciada — testamos apenas a logica de enqueue

    worker.enqueue("primeiro")   # queue: ["primeiro"]
    worker.enqueue("segundo")    # queue: ["primeiro", "segundo"]  — lotada
    worker.enqueue("terceiro")   # overflow: descarta "primeiro" → ["segundo", "terceiro"]

    items = []
    while not worker._queue.empty():
        items.append(worker._queue.get_nowait())

    assert items == ["segundo", "terceiro"], (
        f"Esperado ['segundo', 'terceiro'], obtido {items}"
    )


def test_enqueue_noop_after_shutdown():
    """enqueue() nao deve aceitar itens apos shutdown() ser chamado."""
    worker = LLMWorker(llm=None, state=None, renderer=_FakeRenderer(), queue_maxsize=10)
    # thread NAO iniciada

    worker.enqueue("antes")
    worker._stop.set()           # simula shutdown em andamento
    worker.enqueue("depois")     # deve ser ignorado

    items = []
    while not worker._queue.empty():
        items.append(worker._queue.get_nowait())

    assert items == ["antes"], f"Item apos stop nao deve ser enfileirado: {items}"


# ── erro no LLM nao trava o worker ───────────────────────────────────────────


def test_worker_continues_after_llm_error(monkeypatch):
    """Erro em uma chamada LLM nao deve travar o worker — proximo item e processado."""
    call_results: list[str] = []

    def query_with_error(text, llm, state):
        if text == "erro":
            raise RuntimeError("LLM falhou")
        call_results.append(text)
        return f"resp:{text}", "model", 1.0

    worker, renderer = _make_worker(monkeypatch, query_with_error)
    worker.start()
    worker.enqueue("erro")
    worker.enqueue("ok")

    clean = worker.shutdown(per_call_timeout_s=5.0)

    assert clean
    assert "ok" in call_results
    assert len(renderer.errors) == 1
    assert isinstance(renderer.errors[0], RuntimeError)
