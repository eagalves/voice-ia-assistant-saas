"""Generate pt-BR speech fixtures for real STT transcription tests.

Uses the Windows SAPI voice "Microsoft Maria Desktop" (pt-BR) to synthesize
WAV files with known ground-truth text, then derives robustness variants
(quiet capture, noisy capture, silence, pure noise) with numpy.

The generated files are committed under tests/fixtures/audio/ together with
manifest.json so the test suite runs on any machine — this script only needs
to be re-run when phrases change, and requires Windows with the pt-BR voice.

Usage:
    .venv\\Scripts\\python.exe scripts\\generate_stt_fixtures.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"
VOICE = "Microsoft Maria Desktop"

# Medical-domain pt-BR phrases (the project's target register) plus one
# scheduling phrase. Numbers, drug names and symptoms on purpose — they are
# what a medical STT must not get wrong.
PHRASES: list[tuple[str, str]] = [
    ("dor_cabeca", "O paciente relata dor de cabeça intensa há três dias e náusea pela manhã."),
    ("falta_ar", "Estou sentindo falta de ar quando subo escadas e o coração dispara."),
    ("pressao", "A pressão arterial está em quatorze por nove e a glicemia em jejum deu cento e dez."),
    ("medicacao", "Tomo losartana cinquenta miligramas de manhã e metformina depois do almoço."),
    ("febre", "A febre começou ontem à noite, junto com dor de garganta e calafrios."),
    ("consulta", "Preciso remarcar minha consulta de retorno para a próxima quinta-feira às dez horas."),
]

# Phrases that also get high-sample-rate bases (exercise the resampling path).
MULTIRATE_IDS = {"pressao", "medicacao"}
# Phrases that also get degraded variants (exercise conditioning/robustness).
DEGRADED_IDS = {"dor_cabeca", "medicacao"}

_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$jobs = Get-Content -Raw -Encoding UTF8 '{jobs_path}' | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('{voice}')
foreach ($job in $jobs) {{
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        [int]$job.sample_rate,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($job.path, $fmt)
    $synth.Speak($job.text)
    $synth.SetOutputToNull()
}}
$synth.Dispose()
Write-Output "OK: $($jobs.Count) files"
"""


def synthesize(jobs: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as td:
        jobs_path = Path(td) / "jobs.json"
        jobs_path.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        script_path = Path(td) / "synth.ps1"
        script = _PS_TEMPLATE.format(jobs_path=jobs_path, voice=VOICE)
        script_path.write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))  # UTF-8 BOM
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script_path)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"TTS synthesis failed:\n{result.stdout}\n{result.stderr}")
        print(result.stdout.strip())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1 and wf.getsampwidth() == 2
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        return data, wf.getframerate()


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def scale_int16(samples: np.ndarray, target_peak: float) -> np.ndarray:
    peak = np.max(np.abs(samples.astype(np.float64)))
    if peak == 0:
        return samples
    return np.clip(samples.astype(np.float64) * (target_peak * 32767.0 / peak),
                   -32768, 32767).astype(np.int16)


def add_noise(samples: np.ndarray, snr_db: float, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    signal = samples.astype(np.float64)
    signal_power = float(np.mean(signal**2))
    noise_power = signal_power / (10 ** (snr_db / 10))
    noise = rng.normal(0.0, np.sqrt(noise_power), size=signal.shape)
    return np.clip(signal + noise, -32768, 32767).astype(np.int16)


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    jobs: list[dict] = []

    for pid, text in PHRASES:
        rates = [16000] + ([48000, 44100] if pid in MULTIRATE_IDS else [])
        for rate in rates:
            tag = f"{rate // 1000}k" if rate % 1000 == 0 else "44k1"
            fname = f"{pid}_{tag}.wav"
            jobs.append({"path": str(FIXTURES_DIR / fname), "text": text,
                         "sample_rate": rate})
            manifest.append({"id": pid, "file": fname, "sample_rate": rate,
                             "variant": "clean", "text": text})

    synthesize(jobs)

    # Derived degraded variants from the 16 kHz bases.
    for pid, text in PHRASES:
        if pid not in DEGRADED_IDS:
            continue
        base, rate = read_wav(FIXTURES_DIR / f"{pid}_16k.wav")

        quiet_name = f"{pid}_16k_quiet.wav"
        write_wav(FIXTURES_DIR / quiet_name, scale_int16(base, 0.03), rate)
        manifest.append({"id": pid, "file": quiet_name, "sample_rate": rate,
                         "variant": "quiet", "text": text})

        noisy_name = f"{pid}_16k_noisy.wav"
        write_wav(FIXTURES_DIR / noisy_name, add_noise(base, snr_db=12.0), rate)
        manifest.append({"id": pid, "file": noisy_name, "sample_rate": rate,
                         "variant": "noisy", "text": text})

    # Non-speech fixtures: digital silence and mic-like noise floor.
    rng = np.random.default_rng(7)
    write_wav(FIXTURES_DIR / "silencio_16k.wav",
              np.zeros(16000 * 3, dtype=np.int16), 16000)
    manifest.append({"id": "silencio", "file": "silencio_16k.wav",
                     "sample_rate": 16000, "variant": "silence", "text": ""})

    noise_floor = (rng.normal(0.0, 300.0, size=16000 * 3)).astype(np.int16)
    write_wav(FIXTURES_DIR / "ruido_16k.wav", noise_floor, 16000)
    manifest.append({"id": "ruido", "file": "ruido_16k.wav",
                     "sample_rate": 16000, "variant": "noise_only", "text": ""})

    (FIXTURES_DIR / "manifest.json").write_text(
        json.dumps({"voice": VOICE, "items": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(manifest)} fixture entries to {FIXTURES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
