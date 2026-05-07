"""Issue 26 – Cache do Modelo Whisper em Memoria entre Sessoes.

Garante que:
- Dois backends com mesma config compartilham o mesmo objeto WhisperModel.
- WhisperModel e carregado do disco apenas uma vez por config por processo.
- Log INFO com tempo de carregamento aparece apenas na primeira carga.
- Backends com configs diferentes carregam modelos distintos.
- Cache limpo permite nova carga (simula reinicio de processo).
- Carga concorrente da mesma config e thread-safe: modelo carregado uma unica vez.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from assistente_voz.stt import FasterWhisperBackend, clear_model_cache


# ── fixture: limpa cache antes de cada teste ──────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_cache():
    """Garante isolamento entre testes limpando o cache antes de cada um."""
    clear_model_cache()
    yield
    clear_model_cache()


# ── helper ────────────────────────────────────────────────────────────────────


def _make_backend(model_size: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
    return FasterWhisperBackend(model_size=model_size, device=device, compute_type=compute_type)


def _mock_whisper():
    """Retorna patch context para faster_whisper.WhisperModel."""
    return patch("faster_whisper.WhisperModel", return_value=MagicMock())


# ── caching ───────────────────────────────────────────────────────────────────


def test_second_backend_same_config_does_not_reload_model():
    """Dois backends com mesma config devem chamar WhisperModel() apenas uma vez."""
    with _mock_whisper() as mock_cls:
        b1 = _make_backend()
        b2 = _make_backend()
        b1._load()
        b2._load()

    assert mock_cls.call_count == 1


def test_both_backends_share_same_model_instance():
    """Backends com mesma config devem referenciar o mesmo objeto de modelo."""
    with _mock_whisper():
        b1 = _make_backend()
        b2 = _make_backend()
        m1 = b1._load()
        m2 = b2._load()

    assert m1 is m2


def test_different_model_sizes_cached_separately():
    """Backends com model_size diferente devem carregar modelos distintos."""
    with _mock_whisper() as mock_cls:
        b_tiny = _make_backend(model_size="tiny")
        b_base = _make_backend(model_size="base")
        b_tiny._load()
        b_base._load()

    assert mock_cls.call_count == 2
    sizes_loaded = [c.args[0] for c in mock_cls.call_args_list]
    assert "tiny" in sizes_loaded
    assert "base" in sizes_loaded


def test_different_devices_cached_separately():
    """Backends com device diferente devem carregar modelos distintos."""
    with _mock_whisper() as mock_cls:
        _make_backend(device="cpu")._load()
        _make_backend(device="cuda")._load()

    assert mock_cls.call_count == 2


def test_different_compute_types_cached_separately():
    """Backends com compute_type diferente devem carregar modelos distintos."""
    with _mock_whisper() as mock_cls:
        _make_backend(compute_type="int8")._load()
        _make_backend(compute_type="float16")._load()

    assert mock_cls.call_count == 2


def test_third_backend_same_config_still_reuses():
    """Cache deve funcionar para N instancias, nao apenas 2."""
    with _mock_whisper() as mock_cls:
        for _ in range(5):
            _make_backend()._load()

    assert mock_cls.call_count == 1


# ── logging ───────────────────────────────────────────────────────────────────


def test_load_time_logged_on_first_load(caplog):
    """INFO com tempo de carregamento deve ser emitido na primeira carga."""
    with _mock_whisper():
        with caplog.at_level(logging.INFO, logger="assistente_voz.stt"):
            _make_backend()._load()

    load_msgs = [m for m in caplog.messages if "loaded in" in m]
    assert load_msgs, "Esperado log INFO de tempo de carregamento na primeira carga"


def test_no_load_log_on_second_backend(caplog):
    """Log de carregamento nao deve aparecer para backends que reutilizam cache."""
    with _mock_whisper():
        with caplog.at_level(logging.INFO, logger="assistente_voz.stt"):
            _make_backend()._load()   # primeira carga — gera log
            load_msgs_after_first = [m for m in caplog.messages if "loaded in" in m]

            _make_backend()._load()   # cache hit — nao deve gerar novo log
            load_msgs_after_second = [m for m in caplog.messages if "loaded in" in m]

    assert len(load_msgs_after_first) == 1
    assert len(load_msgs_after_second) == 1  # sem log adicional


def test_log_mentions_model_size(caplog):
    """Log INFO deve mencionar o tamanho do modelo carregado."""
    with _mock_whisper():
        with caplog.at_level(logging.INFO, logger="assistente_voz.stt"):
            _make_backend(model_size="small")._load()

    assert any("small" in m for m in caplog.messages)


# ── clear_model_cache ─────────────────────────────────────────────────────────


def test_clear_cache_forces_fresh_load():
    """Apos clear_model_cache, proxima carga deve instanciar WhisperModel novamente."""
    with _mock_whisper() as mock_cls:
        _make_backend()._load()
        clear_model_cache()
        _make_backend()._load()

    assert mock_cls.call_count == 2


def test_clear_cache_new_instance_does_not_reuse_old_model():
    """Modelo do backend antigo nao deve ser o mesmo apos clear e recarga."""
    with _mock_whisper() as mock_cls:
        mock_cls.side_effect = [MagicMock(name="model_v1"), MagicMock(name="model_v2")]

        b1 = _make_backend()
        m1 = b1._load()

        clear_model_cache()

        b2 = _make_backend()
        m2 = b2._load()

    assert m1 is not m2


# ── thread safety ─────────────────────────────────────────────────────────────


def test_concurrent_loads_same_config_load_model_once():
    """Cargas concorrentes da mesma config devem resultar em apenas uma instancia."""
    n_threads = 10
    barrier = threading.Barrier(n_threads)
    results: list = []
    lock = threading.Lock()

    def load_in_thread():
        backend = _make_backend()
        barrier.wait()  # sincroniza: todos chamam _load() simultaneamente
        model = backend._load()
        with lock:
            results.append(model)

    with _mock_whisper() as mock_cls:
        threads = [threading.Thread(target=load_in_thread) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert mock_cls.call_count == 1, (
        f"WhisperModel deve ser instanciado apenas uma vez; foi {mock_cls.call_count}x"
    )
    assert len(results) == n_threads
    first = results[0]
    assert all(r is first for r in results), "Todas as threads devem receber o mesmo objeto"
