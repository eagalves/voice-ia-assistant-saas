"""Pré-processamento de áudio e guardas de qualidade do STT (sem modelo real).

Garante que:
- Resample 48k→16k aplica filtro anti-aliasing: tom acima de 8 kHz não vaza
  (dobrado) para a banda de fala; tom dentro da banda é preservado.
- Contrato de tamanho do resample é mantido (~n*16000/sr).
- _condition_audio remove DC offset e normaliza áudio baixo sem amplificar
  silêncio digital.
- Filtro de alucinações pt-BR derruba boilerplate de legenda (tier 1 sempre;
  tier 2 apenas com no_speech_prob alto) e nunca derruba fala legítima.
- parse_temperature_env aceita valor único, CSV e cai na escada padrão para
  entradas inválidas.
- Normalização pt-BR e WER/CER do stt_eval se comportam como esperado.
"""
from __future__ import annotations

import numpy as np
import pytest

from assistente_voz.stt import (
    DEFAULT_TEMPERATURE_LADDER,
    _condition_audio,
    _is_hallucinated_segment,
    _to_float32,
    parse_temperature_env,
)
from assistente_voz.stt_eval import (
    char_error_rate,
    contains_term,
    normalize_pt_text,
    word_error_rate,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _sine(freq_hz: float, sr: int, seconds: float = 1.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def _band_power(arr: np.ndarray, sr: int, center_hz: float, width_hz: float = 200.0) -> float:
    spectrum = np.abs(np.fft.rfft(arr)) ** 2
    freqs = np.fft.rfftfreq(arr.size, d=1.0 / sr)
    mask = (freqs >= center_hz - width_hz) & (freqs <= center_hz + width_hz)
    return float(spectrum[mask].sum())


# ── resampling anti-alias ─────────────────────────────────────────────────────


def test_downsample_preserves_speech_band_tone():
    """Tom de 1 kHz (banda de fala) deve sobreviver ao resample 48k→16k."""
    tone = _sine(1000, 48000)
    out = _to_float32(tone, sample_rate=48000)
    power_in_band = _band_power(out, 16000, 1000)
    total = float((np.abs(np.fft.rfft(out)) ** 2).sum())
    assert power_in_band / total > 0.95


def test_downsample_attenuates_aliasing_tone():
    """Tom de 12 kHz @ 48k dobraria para 4 kHz após decimação sem filtro.

    Com o FIR anti-aliasing, a energia em 4 kHz deve ser ordens de magnitude
    menor que a de um tom legítimo de 4 kHz.
    """
    alias_source = _sine(12000, 48000)
    legit_source = _sine(4000, 48000)

    aliased = _to_float32(alias_source, sample_rate=48000)
    legit = _to_float32(legit_source, sample_rate=48000)

    alias_power = _band_power(aliased, 16000, 4000)
    legit_power = _band_power(legit, 16000, 4000)
    assert alias_power < legit_power * 0.01, (
        f"aliasing insuficientemente atenuado: {alias_power:.2e} vs {legit_power:.2e}"
    )


@pytest.mark.parametrize("sr", [48000, 44100, 32000, 22050])
def test_downsample_length_contract(sr):
    """Saída deve ter ~n*16000/sr amostras para qualquer taxa suportada."""
    n_in = sr  # 1 segundo
    out = _to_float32(np.zeros(n_in, dtype=np.float32), sample_rate=sr)
    assert abs(out.size - 16000) <= 2


def test_upsample_from_8k_still_works():
    """Taxas abaixo de 16 kHz (upsample) não passam pelo filtro e mantêm contrato."""
    out = _to_float32(np.zeros(8000, dtype=np.float32), sample_rate=8000)
    assert abs(out.size - 16000) <= 2


# ── condicionamento (DC + normalização) ───────────────────────────────────────


def test_condition_audio_removes_dc_offset():
    arr = np.full(16000, 0.25, dtype=np.float32) + _sine(440, 16000, amp=0.1)
    out = _condition_audio(arr)
    assert abs(float(out.mean())) < 0.01


def test_condition_audio_boosts_quiet_capture():
    quiet = _sine(440, 16000, amp=0.02)
    out = _condition_audio(quiet)
    assert 0.7 < float(np.max(np.abs(out))) <= 1.0


def test_condition_audio_does_not_amplify_digital_silence():
    silence = np.zeros(16000, dtype=np.float32)
    out = _condition_audio(silence)
    assert float(np.max(np.abs(out))) == 0.0


def test_condition_audio_caps_gain_on_noise_floor():
    """Piso de ruído minúsculo não pode ser amplificado até virar 'fala'."""
    floor = _sine(440, 16000, amp=0.00005)  # abaixo do min_peak de 1e-4
    out = _condition_audio(floor)
    assert float(np.max(np.abs(out))) < 0.001


def test_condition_audio_leaves_healthy_audio_untouched():
    healthy = _sine(440, 16000, amp=0.96)
    out = _condition_audio(healthy)
    assert np.allclose(out, healthy, atol=1e-6)


# ── filtro de alucinações pt-BR ───────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Legendas pela comunidade Amara.org",
    "legendas pela comunidade amara.org",
    "Transcrição e legendas pela comunidade Amara.org",
])
def test_amara_boilerplate_always_dropped(text):
    assert _is_hallucinated_segment(text, no_speech_prob=0.0)


def test_suspect_closer_dropped_only_with_high_no_speech():
    assert _is_hallucinated_segment("Obrigado por assistir!", no_speech_prob=0.9)
    assert not _is_hallucinated_segment("Obrigado por assistir!", no_speech_prob=0.1)


@pytest.mark.parametrize("text", [
    "O paciente relata dor de cabeça há três dias.",
    "Tomo losartana cinquenta miligramas de manhã.",
    "Obrigado doutor, até a próxima consulta.",
])
def test_legitimate_speech_never_dropped(text):
    assert not _is_hallucinated_segment(text, no_speech_prob=0.9)


# ── parse_temperature_env ─────────────────────────────────────────────────────


def test_temperature_env_default_is_full_ladder():
    assert parse_temperature_env(None) == DEFAULT_TEMPERATURE_LADDER
    assert parse_temperature_env("") == DEFAULT_TEMPERATURE_LADDER


def test_temperature_env_single_value():
    assert parse_temperature_env("0.0") == 0.0


def test_temperature_env_csv_ladder():
    assert parse_temperature_env("0.0,0.2,0.4") == (0.0, 0.2, 0.4)


def test_temperature_env_invalid_falls_back_to_ladder(caplog):
    assert parse_temperature_env("quente") == DEFAULT_TEMPERATURE_LADDER


# ── stt_eval: normalização e métricas ─────────────────────────────────────────


def test_normalize_folds_numbers_and_units():
    assert normalize_pt_text("Tomo losartana 50mg de manhã.") == \
        normalize_pt_text("Tomo losartana cinquenta miligramas de manhã.")


def test_normalize_strips_accents_and_punctuation():
    assert normalize_pt_text("Pressão, glicemia; coração!") == "pressao glicemia coracao"


def test_wer_zero_for_equivalent_transcriptions():
    ref = "A pressão está em quatorze por nove."
    hyp = "a pressao esta em 14 por 9"
    assert word_error_rate(ref, hyp) == 0.0


def test_wer_counts_substitutions():
    assert word_error_rate("dor de cabeça", "dor de garganta") == pytest.approx(1 / 3)


def test_wer_empty_hypothesis_is_total_error():
    assert word_error_rate("dor de cabeça", "") == 1.0


def test_cer_detects_small_typos():
    cer = char_error_rate("metformina", "metfornina")
    assert 0.0 < cer < 0.2


def test_contains_term_robust_to_token_splits():
    assert contains_term("Tomo los artanas 50 mg", "losartana")
    assert not contains_term("Tomo metformina", "losartana")
