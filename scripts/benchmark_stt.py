"""Benchmark local Whisper models on the pt-BR fixture set.

Measures per model: WER, CER, mean confidence, latency and RTF (real-time
factor: transcription_time / audio_duration) across fixture variants
(clean, quiet, noisy, resampled, silence).

Usage:
    .venv\\Scripts\\python.exe scripts\\benchmark_stt.py
    .venv\\Scripts\\python.exe scripts\\benchmark_stt.py --models tiny,base,small,medium
    .venv\\Scripts\\python.exe scripts\\benchmark_stt.py --models small --ab-baseline
    .venv\\Scripts\\python.exe scripts\\benchmark_stt.py --json out.json

--ab-baseline runs the pre-improvement configuration (single temperature 0.0,
no audio conditioning, no hallucination filter) for before/after comparison.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from assistente_voz.stt import DEFAULT_TEMPERATURE_LADDER, FasterWhisperBackend  # noqa: E402
from assistente_voz.stt_eval import char_error_rate, word_error_rate  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"


def read_wav_bytes(path: Path) -> tuple[bytes, int, float]:
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
        duration = wf.getnframes() / rate
        return frames, rate, duration


def run_model(model: str, items: list[dict], args: argparse.Namespace) -> list[dict]:
    backend = FasterWhisperBackend(
        model_size=model,
        device=args.device,
        compute_type=args.compute,
        beam_size=args.beam,
        temperature=0.0 if args.ab_baseline else DEFAULT_TEMPERATURE_LADDER,
        condition_audio=not args.ab_baseline,
        filter_hallucinations=not args.ab_baseline,
        initial_prompt=args.initial_prompt or None,
        cpu_threads=args.cpu_threads,
    )

    # Warm-up: forces model load + first-inference allocations out of the timings.
    import numpy as np
    t0 = time.perf_counter()
    backend.transcribe(np.zeros(16000, dtype=np.float32), language="pt")
    load_s = time.perf_counter() - t0
    print(f"\n[{model}] loaded+warm in {load_s:.1f}s", flush=True)

    records: list[dict] = []
    for item in items:
        audio, rate, duration = read_wav_bytes(FIXTURES_DIR / item["file"])
        t0 = time.perf_counter()
        text, conf = backend.transcribe(audio, language="pt", sample_rate=rate)
        latency = time.perf_counter() - t0
        rec = {
            "model": model,
            "file": item["file"],
            "variant": item["variant"],
            "ref": item["text"],
            "hyp": text,
            "confidence": round(conf, 3),
            "latency_s": round(latency, 2),
            "audio_s": round(duration, 2),
            "rtf": round(latency / duration, 2) if duration else 0.0,
        }
        if item["variant"] in ("silence", "noise_only"):
            rec["hallucinated"] = bool(text.strip())
        else:
            rec["wer"] = round(word_error_rate(item["text"], text), 3)
            rec["cer"] = round(char_error_rate(item["text"], text), 3)
        records.append(rec)
        marker = ""
        if rec.get("hallucinated"):
            marker = f"  !! HALLUCINATION: {text!r}"
        print(f"  {item['file']:28s} wer={rec.get('wer', '-'):<6} conf={conf:.2f} "
              f"rtf={rec['rtf']:.2f}{marker}", flush=True)
    return records


def summarize(records: list[dict]) -> None:
    print("\n" + "=" * 100)
    header = (f"{'model':<9} {'variant':<12} {'n':>2} {'WER':>7} {'CER':>7} "
              f"{'conf':>6} {'RTF':>6} {'lat p95':>8}  halluc")
    print(header)
    print("-" * 100)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in records:
        groups[(rec["model"], rec["variant"])].append(rec)
    for (model, variant), recs in sorted(groups.items()):
        wers = [r["wer"] for r in recs if "wer" in r]
        cers = [r["cer"] for r in recs if "cer" in r]
        lats = sorted(r["latency_s"] for r in recs)
        p95 = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
        halluc = sum(1 for r in recs if r.get("hallucinated"))
        wer_col = f"{statistics.mean(wers):>7.3f}" if wers else f"{'-':>7}"
        cer_col = f"{statistics.mean(cers):>7.3f}" if cers else f"{'-':>7}"
        conf = statistics.mean(r["confidence"] for r in recs)
        rtf = statistics.mean(r["rtf"] for r in recs)
        print(f"{model:<9} {variant:<12} {len(recs):>2} {wer_col} {cer_col} "
              f"{conf:>6.2f} {rtf:>6.2f} {p95:>7.1f}s  {halluc}/{len(recs)}")
    print("=" * 100)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Whisper models on pt-BR fixtures")
    parser.add_argument("--models", default="tiny,small,medium",
                        help="CSV of faster-whisper model sizes/paths")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute", default="int8")
    parser.add_argument("--beam", type=int, default=5)
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--initial-prompt", default=None)
    parser.add_argument("--ab-baseline", action="store_true",
                        help="Run with pre-improvement settings for A/B comparison")
    parser.add_argument("--json", default=None, help="Write raw records to this path")
    args = parser.parse_args()

    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    items = manifest["items"]
    print(f"{len(items)} fixtures | device={args.device} compute={args.compute} "
          f"beam={args.beam} baseline={args.ab_baseline}")

    all_records: list[dict] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        all_records.extend(run_model(model, items, args))

    summarize(all_records)
    if args.json:
        Path(args.json).write_text(
            json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"raw records -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
