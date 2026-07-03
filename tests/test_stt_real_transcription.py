"""Transcrição REAL com faster-whisper sobre fixtures pt-BR (sem mocks).

Roda os modelos Whisper de verdade sobre áudio sintetizado com voz pt-BR
(Microsoft Maria) cujo ground truth é conhecido — ver
scripts/generate_stt_fixtures.py e tests/fixtures/audio/manifest.json.

Garante que:
- WER agregado em áudio limpo fica abaixo do limite por modelo (tiny/small;
  medium opt-in via STT_TEST_HEAVY=1 por ser lento em CPU).
- Cada frase limpa individual fica abaixo de WER 0.25 no modelo padrão.
- Termos clínicos críticos (fármacos, sintomas) aparecem na transcrição.
- Áudio capturado a 48 kHz / 44.1 kHz transcreve tão bem quanto 16 kHz
  (protege o caminho de resample anti-aliasing).
- Captura em volume baixo (peak 0.03) transcreve sem perda (protege a
  normalização de loudness).
- Ruído a 12 dB SNR degrada graciosamente, sem esvaziar a transcrição.
- Silêncio e ruído puro produzem texto vazio — sem alucinação.
- Confiança é calibrada: limpo ≥ 0.55, silêncio = 0.0, limpo > ruidoso.
- STTService end-to-end com backend real: OK para fala, EMPTY para silêncio.

Custo: ~1 min em CPU (tiny + small, modelos já em cache). Pular com
STT_REAL_TESTS=0 ou `-m "not stt_real"`.
"""
from __future__ import annotations

import functools
import json
import os
import statistics
import wave
from pathlib import Path

import pytest

from assistente_voz.stt import FasterWhisperBackend, STTService, TranscriptionStatus
from assistente_voz.stt_eval import contains_term, word_error_rate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "audio"

pytestmark = pytest.mark.stt_real

# Limites calibrados com folga sobre o benchmark (scripts/benchmark_stt.py,
# CPU int8, beam 5). Observado: tiny ~0.29 agregado, small ~0.04.
MODEL_CLEAN_WER_BOUND = {"tiny": 0.60, "small": 0.15, "medium": 0.12}

DEFAULT_MODEL = "small"
HEAVY_MODELS = ("medium",)


def _models_under_test() -> list[str]:
    raw = os.getenv("STT_TEST_MODELS", "tiny,small,medium")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if os.getenv("STT_TEST_HEAVY", "0") not in ("1", "true"):
        models = [m for m in models if m not in HEAVY_MODELS]
    return models


def _model_available(size: str) -> bool:
    """True se o modelo já está no cache local do HuggingFace (não baixa nada)."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(f"Systran/faster-whisper-{size}", local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 — qualquer falha = indisponível offline
        return False


if os.getenv("STT_REAL_TESTS", "1") in ("0", "false"):
    pytest.skip("STT_REAL_TESTS=0", allow_module_level=True)
if not (FIXTURES_DIR / "manifest.json").exists():
    pytest.skip(
        "fixtures ausentes — rode scripts/generate_stt_fixtures.py",
        allow_module_level=True,
    )

MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
ITEMS = {(i["id"], i["variant"], i["sample_rate"]): i for i in MANIFEST["items"]}
CLEAN_16K = [i for i in MANIFEST["items"] if i["variant"] == "clean" and i["sample_rate"] == 16000]


def _read_wav(name: str) -> tuple[bytes, int]:
    with wave.open(str(FIXTURES_DIR / name), "rb") as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate()


@functools.lru_cache(maxsize=None)
def _backend(model: str) -> FasterWhisperBackend:
    return FasterWhisperBackend(model_size=model, device="cpu", compute_type="int8")


@functools.lru_cache(maxsize=None)
def _transcribe(model: str, filename: str) -> tuple[str, float]:
    """Transcreve uma fixture uma única vez por modelo; testes reutilizam."""
    audio, rate = _read_wav(filename)
    return _backend(model).transcribe(audio, language="pt", sample_rate=rate)


def _require_model(model: str) -> None:
    if not _model_available(model):
        pytest.skip(f"modelo {model!r} não está no cache local do HuggingFace")


# ── WER por modelo em áudio limpo ─────────────────────────────────────────────


@pytest.mark.parametrize("model", _models_under_test())
def test_clean_aggregate_wer_within_model_bound(model):
    """WER agregado das frases limpas 16 kHz fica no limite do modelo."""
    _require_model(model)
    bound = MODEL_CLEAN_WER_BOUND.get(model)
    assert bound is not None, f"defina limite de WER para o modelo {model!r}"

    wers = []
    for item in CLEAN_16K:
        text, _conf = _transcribe(model, item["file"])
        wers.append(word_error_rate(item["text"], text))

    aggregate = statistics.mean(wers)
    assert aggregate <= bound, (
        f"{model}: WER agregado {aggregate:.3f} > limite {bound} — detalhes: "
        + "; ".join(f"{i['id']}={w:.2f}" for i, w in zip(CLEAN_16K, wers))
    )


@pytest.mark.parametrize("item", CLEAN_16K, ids=lambda i: i["id"])
def test_clean_phrase_wer_default_model(item):
    """Nenhuma frase limpa individual pode passar de WER 0.25 no modelo padrão."""
    _require_model(DEFAULT_MODEL)
    text, _conf = _transcribe(DEFAULT_MODEL, item["file"])
    wer = word_error_rate(item["text"], text)
    assert wer <= 0.25, f"{item['id']}: WER {wer:.3f}\nref: {item['text']}\nhyp: {text}"


# ── termos clínicos críticos ──────────────────────────────────────────────────


@pytest.mark.parametrize(("fixture_id", "term"), [
    ("dor_cabeca", "dor de cabeça"),
    ("dor_cabeca", "náusea"),
    ("falta_ar", "falta de ar"),
    ("pressao", "pressão arterial"),
    ("pressao", "glicemia"),
    ("medicacao", "losartana"),
    ("medicacao", "metformina"),
    ("febre", "dor de garganta"),
    ("consulta", "quinta-feira"),
])
def test_critical_clinical_terms_transcribed(fixture_id, term):
    """Fármacos e sintomas não podem se perder na transcrição do modelo padrão."""
    _require_model(DEFAULT_MODEL)
    item = ITEMS[(fixture_id, "clean", 16000)]
    text, _conf = _transcribe(DEFAULT_MODEL, item["file"])
    assert contains_term(text, term), f"termo {term!r} ausente em: {text!r}"


# ── robustez a sample rate (caminho de resample) ──────────────────────────────


@pytest.mark.parametrize("fixture_id", ["pressao", "medicacao"])
@pytest.mark.parametrize("rate", [48000, 44100])
def test_high_sample_rate_matches_16k_quality(fixture_id, rate):
    """Áudio 48k/44.1k deve transcrever tão bem quanto o mesmo áudio a 16k."""
    _require_model(DEFAULT_MODEL)
    base = ITEMS[(fixture_id, "clean", 16000)]
    high = ITEMS[(fixture_id, "clean", rate)]

    text_16k, _ = _transcribe(DEFAULT_MODEL, base["file"])
    text_high, _ = _transcribe(DEFAULT_MODEL, high["file"])

    wer_16k = word_error_rate(base["text"], text_16k)
    wer_high = word_error_rate(high["text"], text_high)

    assert wer_high <= 0.25, f"{high['file']}: WER {wer_high:.3f}\nhyp: {text_high}"
    assert wer_high <= wer_16k + 0.15, (
        f"resample degradou: {rate} Hz WER {wer_high:.3f} vs 16 kHz {wer_16k:.3f}"
    )


# ── robustez a captura baixa e ruído ──────────────────────────────────────────


@pytest.mark.parametrize("fixture_id", ["dor_cabeca", "medicacao"])
def test_quiet_capture_transcribes_cleanly(fixture_id):
    """Captura com peak 0.03 (mic baixo) deve transcrever como áudio normal."""
    _require_model(DEFAULT_MODEL)
    item = ITEMS[(fixture_id, "quiet", 16000)]
    text, _conf = _transcribe(DEFAULT_MODEL, item["file"])
    wer = word_error_rate(item["text"], text)
    assert wer <= 0.25, f"{item['file']}: WER {wer:.3f}\nhyp: {text}"


@pytest.mark.parametrize("fixture_id", ["dor_cabeca", "medicacao"])
def test_noisy_capture_degrades_gracefully(fixture_id):
    """Ruído a 12 dB SNR pode degradar, mas não pode esvaziar a transcrição."""
    _require_model(DEFAULT_MODEL)
    item = ITEMS[(fixture_id, "noisy", 16000)]
    text, _conf = _transcribe(DEFAULT_MODEL, item["file"])
    assert text.strip(), "transcrição vazia sob ruído moderado"
    wer = word_error_rate(item["text"], text)
    assert wer <= 0.75, f"{item['file']}: WER {wer:.3f}\nhyp: {text}"


# ── silêncio e ruído puro: sem alucinação ─────────────────────────────────────


@pytest.mark.parametrize("model", _models_under_test())
@pytest.mark.parametrize("fixture_id", ["silencio", "ruido"])
def test_non_speech_produces_no_text(model, fixture_id):
    """Silêncio digital e piso de ruído não podem virar texto alucinado."""
    _require_model(model)
    variant = "silence" if fixture_id == "silencio" else "noise_only"
    item = ITEMS[(fixture_id, variant, 16000)]
    text, conf = _transcribe(model, item["file"])
    assert not text.strip(), f"{model} alucinou em {fixture_id}: {text!r} (conf={conf:.2f})"


# ── calibração de confiança ───────────────────────────────────────────────────


def test_confidence_is_calibrated():
    """Confiança separa fala limpa (≥0.55) de silêncio (0.0) e de ruído."""
    _require_model(DEFAULT_MODEL)
    clean_confs = [_transcribe(DEFAULT_MODEL, i["file"])[1] for i in CLEAN_16K]
    noisy_confs = [
        _transcribe(DEFAULT_MODEL, ITEMS[(fid, "noisy", 16000)]["file"])[1]
        for fid in ("dor_cabeca", "medicacao")
    ]
    _text, silence_conf = _transcribe(
        DEFAULT_MODEL, ITEMS[("silencio", "silence", 16000)]["file"]
    )

    assert statistics.mean(clean_confs) >= 0.55
    assert min(clean_confs) >= 0.45
    assert silence_conf == 0.0
    assert statistics.mean(clean_confs) > statistics.mean(noisy_confs)


# ── initial_prompt: ganho de acurácia sem eco em silêncio ─────────────────────

CLINICAL_PROMPT = (
    "Consulta médica em português do Brasil. Paciente relata sintomas, "
    "medicamentos e histórico clínico."
)


def _prompted_backend() -> FasterWhisperBackend:
    return FasterWhisperBackend(
        model_size=DEFAULT_MODEL, device="cpu", compute_type="int8",
        initial_prompt=CLINICAL_PROMPT, hotwords="losartana, metformina, dipirona",
    )


def test_initial_prompt_does_not_hurt_medication_accuracy():
    """Priming clínico deve manter ou melhorar a frase com fármacos."""
    _require_model(DEFAULT_MODEL)
    item = ITEMS[("medicacao", "clean", 16000)]
    audio, rate = _read_wav(item["file"])

    text_prompted, _ = _prompted_backend().transcribe(audio, language="pt", sample_rate=rate)
    text_plain, _ = _transcribe(DEFAULT_MODEL, item["file"])

    wer_prompted = word_error_rate(item["text"], text_prompted)
    wer_plain = word_error_rate(item["text"], text_plain)
    assert wer_prompted <= wer_plain, (
        f"prompt piorou: {wer_prompted:.3f} > {wer_plain:.3f}\nhyp: {text_prompted}"
    )
    assert contains_term(text_prompted, "losartana")
    assert contains_term(text_prompted, "metformina")


def test_initial_prompt_echo_suppressed_on_silence():
    """Em silêncio, o decoder tende a ecoar o initial_prompt — deve ser suprimido."""
    _require_model(DEFAULT_MODEL)
    audio, rate = _read_wav(ITEMS[("silencio", "silence", 16000)]["file"])
    text, conf = _prompted_backend().transcribe(audio, language="pt", sample_rate=rate)
    assert not text.strip(), f"eco de prompt não suprimido: {text!r} (conf={conf:.2f})"


# ── STTService end-to-end com backend real ────────────────────────────────────


def test_sttservice_end_to_end_ok_for_clean_speech():
    """Pipeline completo (política de confiança + retry) sobre áudio real."""
    _require_model(DEFAULT_MODEL)
    svc = STTService(
        backend=_backend(DEFAULT_MODEL), confidence_threshold=0.55, max_retries=1
    )
    audio, rate = _read_wav(ITEMS[("falta_ar", "clean", 16000)]["file"])
    outcome = svc.transcribe(audio, sample_rate=rate)

    assert outcome.status == TranscriptionStatus.OK
    assert outcome.result is not None
    ref = ITEMS[("falta_ar", "clean", 16000)]["text"]
    assert word_error_rate(ref, outcome.result.text) <= 0.25


def test_sttservice_end_to_end_empty_for_silence():
    _require_model(DEFAULT_MODEL)
    svc = STTService(
        backend=_backend(DEFAULT_MODEL), confidence_threshold=0.55, max_retries=1
    )
    audio, rate = _read_wav(ITEMS[("silencio", "silence", 16000)]["file"])
    outcome = svc.transcribe(audio, sample_rate=rate)

    assert outcome.status == TranscriptionStatus.EMPTY
    assert outcome.result is None
