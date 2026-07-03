"""Evaluation helpers for STT quality: pt-BR normalization, WER and CER.

Used by the real-transcription test suite (tests/test_stt_real_transcription.py)
and the model benchmark (scripts/benchmark_stt.py). Dependency-free on purpose
(no jiwer) so it can run anywhere the app runs.

Normalization philosophy: WER must measure *transcription* errors, not
orthographic style. Whisper legitimately writes "50 mg" where the speaker said
"cinquenta miligramas" — both are correct transcriptions. So we fold numbers,
units and known spelling variants to a canonical form before comparing.
"""
from __future__ import annotations

import re
import unicodedata

# Digit → extenso (accent-stripped) for numbers that appear in clinical speech.
# Unknown numbers stay as digits on both sides, which still compares fairly.
_NUM_WORDS: dict[str, str] = {
    "0": "zero", "1": "um", "2": "dois", "3": "tres", "4": "quatro",
    "5": "cinco", "6": "seis", "7": "sete", "8": "oito", "9": "nove",
    "10": "dez", "11": "onze", "12": "doze", "13": "treze", "14": "quatorze",
    "15": "quinze", "16": "dezesseis", "17": "dezessete", "18": "dezoito",
    "19": "dezenove", "20": "vinte", "30": "trinta", "40": "quarenta",
    "50": "cinquenta", "60": "sessenta", "70": "setenta", "80": "oitenta",
    "90": "noventa", "100": "cem", "110": "cento e dez", "120": "cento e vinte",
    "200": "duzentos", "500": "quinhentos", "1000": "mil",
}

# Orthographic/abbreviation variants folded to one canonical form.
_SYNONYMS: dict[str, str] = {
    "catorze": "quatorze",
    "mg": "miligramas",
    "miligrama": "miligramas",
    "ml": "mililitros",
    "mililitro": "mililitros",
    "kg": "quilos",
    "km": "quilometros",
    "hrs": "horas",
    "h": "horas",
    "dr": "doutor",
    "dra": "doutora",
}


def normalize_pt_text(text: str) -> str:
    """Canonical form for comparison: lowercase, accent-free, no punctuation,
    numbers and units expanded, known variants folded."""
    lowered = unicodedata.normalize("NFKD", text.lower())
    lowered = "".join(c for c in lowered if not unicodedata.combining(c))
    # split digit-unit collocations ("50mg" → "50 mg") before tokenizing
    lowered = re.sub(r"(\d)([a-z])", r"\1 \2", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    tokens: list[str] = []
    for tok in lowered.split():
        tok = _SYNONYMS.get(tok, tok)
        if tok.isdigit() and tok in _NUM_WORDS:
            tokens.extend(_NUM_WORDS[tok].split())
        else:
            tokens.append(tok)
    return " ".join(tokens)


def _levenshtein(ref: list, hyp: list) -> int:
    """Classic DP edit distance over any sequence of comparable items."""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr[j] = min(prev[j] + 1,        # deletion
                          curr[j - 1] + 1,    # insertion
                          prev[j - 1] + cost) # substitution
        prev = curr
    return prev[-1]


def word_edit_distance(reference: str, hypothesis: str) -> int:
    """Distância de edição em palavras após normalização pt-BR."""
    ref = normalize_pt_text(reference).split()
    hyp = normalize_pt_text(hypothesis).split()
    return _levenshtein(ref, hyp)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER after pt-BR normalization. Empty reference → 0.0 if hyp empty, else 1.0."""
    ref = normalize_pt_text(reference).split()
    hyp = normalize_pt_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def char_error_rate(reference: str, hypothesis: str) -> float:
    """CER after pt-BR normalization (spaces excluded)."""
    ref = list(normalize_pt_text(reference).replace(" ", ""))
    hyp = list(normalize_pt_text(hypothesis).replace(" ", ""))
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def phonetic_similarity(a: str, b: str) -> float:
    """Similaridade 0–1 em nível de caractere entre as formas normalizadas.

    Proxy barato para "soa parecido": erros reais de ASR são foneticamente
    próximos do que foi dito ("lousa há tanas" ≈ "losartana" → ~0.6), enquanto
    reescritas de estilo não são ("de manhã" vs "ao acordar" → ~0.1).
    """
    na = normalize_pt_text(a).replace(" ", "")
    nb = normalize_pt_text(b).replace(" ", "")
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return 1.0 - _levenshtein(list(na), list(nb)) / max(len(na), len(nb))


def contains_term(hypothesis: str, term: str) -> bool:
    """True if *term* appears in *hypothesis* ignoring spacing/accents/case.

    Robust to tokenization splits ("losartana" found inside "los artanas 50").
    """
    hyp = normalize_pt_text(hypothesis).replace(" ", "")
    needle = normalize_pt_text(term).replace(" ", "")
    return bool(needle) and needle in hyp
