"""Voice CLI — STT pipeline tuned for completude (capture everything spoken).

Default mode:
    python -m assistente_voz.voice_cli --duration 60
    -> Live captioning is OFF.  Each utterance is transcribed once it ends and
       printed on its own line with timestamp/duration/confidence.  Slower per
       utterance but maximises completude (no missed phrases, no hallucinated
       partials confusing the output).

Live caption mode (lower latency, partial transcriptions):
    python -m assistente_voz.voice_cli --duration 60 --live-caption

Other flags:
    --duration       seconds to listen (0 = until Ctrl+C)
    --no-tts         disable TTS playback (TTS not wired in this build)
    --debug-audio    print VAD RMS + mic status for tuning
    --list-devices   list input-capable devices and exit
    --input-device   device index or substring of name
    --caption-window seconds of audio kept visible in the live caption display
    --text           bypass STT, send this text straight to the LLM
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from typing import Optional

from assistente_voz.audio_capture import MicrophoneSource
from assistente_voz.bootstrap import bootstrap_app
from assistente_voz.conversation_state import ConversationState
from assistente_voz.llm import LLMService
from assistente_voz.live_caption import (
    DEFAULT_HANG_MS,
    DEFAULT_PARTIAL_INTERVAL_MS,
    DEFAULT_PREROLL_MS,
    DEFAULT_VAD_RMS,
    CaptionEvent,
    StreamingTranscriber,
)
from assistente_voz.stt import FasterWhisperBackend


# ── argparse ──────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voice_cli", description="Voice CLI — completude-first STT")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Seconds to listen (0 = until Ctrl+C)")
    p.add_argument("--no-tts", action="store_true",
                   help="Disable TTS playback (TTS not wired in this build)")
    p.add_argument("--debug-audio", action="store_true",
                   help="Print VAD RMS + sounddevice status for tuning")
    p.add_argument("--list-devices", action="store_true",
                   help="List input-capable devices and exit")
    p.add_argument("--input-device", default=None,
                   help="Device index or substring of name")
    p.add_argument("--live-caption", action="store_true", default=False,
                   help="Enable live partial captions (default: OFF — completude-first)")
    p.add_argument("--no-live-caption", dest="live_caption", action="store_false",
                   help="Force live caption off (default behaviour)")
    p.add_argument("--caption-window", type=float, default=8.0,
                   help="Seconds of audio kept visible in the live caption display")
    p.add_argument("--text", default=None,
                   help="Bypass STT and send this text straight to the LLM")

    # tuning knobs (sane defaults; override only when needed)
    p.add_argument("--vad-rms", type=float, default=DEFAULT_VAD_RMS,
                   help="Energy threshold for VAD (int16 RMS). Lower = catches softer speech")
    p.add_argument("--partial-interval-ms", type=int, default=DEFAULT_PARTIAL_INTERVAL_MS,
                   help="How often to publish a live caption (ms) — only with --live-caption")
    p.add_argument("--hang-ms", type=int, default=DEFAULT_HANG_MS,
                   help="Silence required to close an utterance (ms). Higher = groups long pauses")
    p.add_argument("--preroll-ms", type=int, default=DEFAULT_PREROLL_MS,
                   help="Audio kept before VAD opens an utterance (ms)")

    return p


# ── helpers ───────────────────────────────────────────────────────────────────


def _resolve_device(value: Optional[str]):
    """Translate a user-provided device hint to an index sounddevice accepts."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    for d in MicrophoneSource.list_devices():
        if value.lower() in d["name"].lower():
            return d["index"]
    raise SystemExit(f"No input device matches: {value!r}")


def _print_devices() -> int:
    devs = MicrophoneSource.list_devices()
    if not devs:
        print("No input-capable devices detected.")
        return 1
    for d in devs:
        print(f"  [{d['index']:>3}] {d['name']}  "
              f"(sr={d['default_samplerate']}, ch={d['max_input_channels']})")
    return 0


def _make_final_backend(debug: bool) -> FasterWhisperBackend:
    """Build the final-pass model — the only model needed in completude mode."""
    shared_default = os.getenv("WHISPER_MODEL")
    final_model = os.getenv("WHISPER_FINAL_MODEL", shared_default or "small")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE", "int8")

    backend = FasterWhisperBackend(
        model_size=final_model, device=device, compute_type=compute_type,
        beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "5")),
        vad_filter=False, condition_on_previous_text=False,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        temperature=0.0,
    )
    if debug:
        print(f"[stt] final={final_model} device={device} compute={compute_type}", flush=True)
    return backend


def _make_partial_backend(debug: bool) -> FasterWhisperBackend:
    """Build the partial-pass model — only used with --live-caption."""
    shared_default = os.getenv("WHISPER_MODEL")
    partial_model = os.getenv("WHISPER_PARTIAL_MODEL", shared_default or "tiny")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE", "int8")

    backend = FasterWhisperBackend(
        model_size=partial_model, device=device, compute_type=compute_type,
        beam_size=1, vad_filter=False, condition_on_previous_text=False,
        no_speech_threshold=0.65,
        log_prob_threshold=-1.0,
        temperature=0.0,
    )
    if debug:
        print(f"[stt] partial={partial_model}", flush=True)
    return backend


def _warmup(backend: FasterWhisperBackend, label: str, debug: bool) -> None:
    """Force model load by transcribing 1s of silence so the first real
    utterance doesn't pay the load-time cost."""
    import numpy as np

    if debug:
        print(f"[stt] warming up {label}…", flush=True)
    t0 = time.perf_counter()
    try:
        backend.transcribe(np.zeros(16000, dtype=np.float32), language="pt")
    except Exception as exc:  # noqa: BLE001
        print(f"[stt] warmup failed for {label}: {exc}", flush=True)
        return
    if debug:
        print(f"[stt] {label} ready in {time.perf_counter() - t0:.1f}s", flush=True)


def _build_llm_runtime() -> tuple[LLMService, ConversationState]:
    """Build LLM runtime from env + profile and prepare conversation state."""
    import yaml

    boot = bootstrap_app()
    profile_data = yaml.safe_load(boot.profile_path.read_text(encoding="utf-8")) or {}
    state = ConversationState(max_messages=int(os.getenv("CONVERSATION_MAX_MESSAGES", "50")))
    llm = LLMService.from_env(profile_data)
    llm.apply_profile(state)
    return llm, state


def _query_llm_once(text: str, llm: LLMService, state: ConversationState) -> tuple[str, str, float]:
    """Append user text to state, query model, and append assistant reply."""
    state.add(role="user", content=text)
    response = llm.complete(state)
    state.add(role="assistant", content=response.text)
    return response.text, response.model, response.latency_ms


# ── render: completude-first terminal output ─────────────────────────────────


class CompletudeRenderer:
    """Each utterance gets its own line with timestamp / duration / confidence.

    Live-caption (partial) lines sobrescrevem a linha atual via ``\\r``; quando
    o final chega, a linha parcial é apagada e a final é impressa em linha
    própria com prefixo claro.
    """

    def __init__(self, started_at: float, line_width: int = 120) -> None:
        self._lock = threading.Lock()
        self._started_at = started_at
        self._line_width = line_width
        self._utterance_count = 0
        self._partial_dirty = False  # has a partial line been written?

    def __call__(self, ev: CaptionEvent) -> None:
        with self._lock:
            if ev.is_final:
                self._utterance_count += 1
                if self._partial_dirty:
                    sys.stdout.write("\r" + " " * self._line_width + "\r")
                    self._partial_dirty = False
                ts = self._fmt_ts(time.time() - self._started_at)
                dur = ev.audio_ms / 1000.0
                conf_marker = "OK" if ev.confidence >= 0.55 else ("?" if ev.confidence >= 0.30 else "!")
                sys.stdout.write(
                    f"[{ts}  #{self._utterance_count:03d}  {dur:>4.1f}s  "
                    f"conf={ev.confidence:.2f} {conf_marker}] {ev.text}\n"
                )
            else:
                line = f"[live  conf={ev.confidence:.2f}] {ev.text}"
                if len(line) > self._line_width:
                    line = line[: self._line_width - 1] + "…"
                sys.stdout.write("\r" + line.ljust(self._line_width))
                self._partial_dirty = True
            sys.stdout.flush()

    @staticmethod
    def _fmt_ts(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def summary(self) -> None:
        print(f"\n[summary] {self._utterance_count} utterance(s) captured.", flush=True)

    def assistant_reply(self, text: str, model: str, latency_ms: float) -> None:
        with self._lock:
            if self._partial_dirty:
                sys.stdout.write("\r" + " " * self._line_width + "\r")
                self._partial_dirty = False
            sys.stdout.write(f"[assistant model={model} latency={latency_ms:.0f}ms] {text}\n")
            sys.stdout.flush()

    def assistant_error(self, exc: Exception) -> None:
        with self._lock:
            if self._partial_dirty:
                sys.stdout.write("\r" + " " * self._line_width + "\r")
                self._partial_dirty = False
            sys.stdout.write(f"[assistant-error] {type(exc).__name__}: {exc}\n")
            sys.stdout.flush()


# ── modes ─────────────────────────────────────────────────────────────────────


def run_text_mode(text: str) -> int:
    """Bypass STT entirely — useful for smoke-testing the LLM path."""
    try:
        llm, state = _build_llm_runtime()
    except Exception as exc:  # noqa: BLE001
        print(f"[text-error] failed to initialize LLM runtime: {type(exc).__name__}: {exc}")
        return 1

    print(f"[user] {text}")
    try:
        reply, model, latency_ms = _query_llm_once(text=text, llm=llm, state=state)
    except Exception as exc:  # noqa: BLE001
        print(f"[assistant-error] {type(exc).__name__}: {exc}")
        return 1

    print(f"[assistant model={model} latency={latency_ms:.0f}ms] {reply}")
    return 0


def run_voice_mode(args: argparse.Namespace) -> int:
    try:
        llm, state = _build_llm_runtime()
    except Exception as exc:  # noqa: BLE001
        print(f"[voice-error] failed to initialize LLM runtime: {type(exc).__name__}: {exc}")
        return 1

    device = _resolve_device(args.input_device)
    source = MicrophoneSource(
        sample_rate=int(os.getenv("MIC_SAMPLE_RATE", "16000")),
        chunk_frames=int(os.getenv("MIC_CHUNK_FRAMES", "1024")),
        device=device,
        debug=args.debug_audio,
    )

    final_be = _make_final_backend(debug=args.debug_audio)
    partial_be = _make_partial_backend(debug=args.debug_audio) if args.live_caption else None

    line_width = max(80, int(args.caption_window * 15))
    started_at = time.time()
    renderer = CompletudeRenderer(started_at=started_at, line_width=line_width)
    llm_queue: queue.Queue[str] = queue.Queue(maxsize=32)
    llm_stop = threading.Event()

    def _on_caption_event(ev: CaptionEvent) -> None:
        renderer(ev)
        if not ev.is_final:
            return
        text = ev.text.strip()
        if not text:
            return
        try:
            llm_queue.put_nowait(text)
        except queue.Full:
            try:
                llm_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                llm_queue.put_nowait(text)
            except queue.Full:
                pass

    def _llm_worker() -> None:
        while not llm_stop.is_set() or not llm_queue.empty():
            try:
                user_text = llm_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                reply, model, latency_ms = _query_llm_once(text=user_text, llm=llm, state=state)
                renderer.assistant_reply(text=reply, model=model, latency_ms=latency_ms)
            except Exception as exc:  # noqa: BLE001
                renderer.assistant_error(exc)

    # Pre-load models so the first utterance doesn't include the model-load delay.
    _warmup(final_be, "final", args.debug_audio)
    if partial_be is not None:
        _warmup(partial_be, "partial", args.debug_audio)

    transcriber = StreamingTranscriber(
        source=source,
        final_backend=final_be,
        partial_backend=partial_be,
        on_event=_on_caption_event,
        language=os.getenv("STT_LANGUAGE", "pt"),
        partial_interval_ms=args.partial_interval_ms,
        hang_ms=args.hang_ms,
        preroll_ms=args.preroll_ms,
        vad_rms_threshold=args.vad_rms,
        debug_audio=args.debug_audio,
    )

    mode = "live-caption + final" if args.live_caption else "completude (final only)"
    print(f"[voice] mode={mode}  vad_rms={args.vad_rms}  hang={args.hang_ms}ms  "
          f"preroll={args.preroll_ms}ms", flush=True)
    print(f"[voice] listening… speak now (Ctrl+C to stop)", flush=True)

    llm_thread = threading.Thread(target=_llm_worker, name="llm-worker", daemon=True)
    llm_thread.start()

    transcriber.start()
    try:
        if args.duration > 0:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[voice] stopping…", flush=True)
    finally:
        transcriber.stop()
        llm_stop.set()
        llm_thread.join(timeout=15.0)
        if llm_thread.is_alive():
            print("[voice-warning] LLM worker did not finish cleanly before timeout.", flush=True)
        renderer.summary()
    return 0


# ── entrypoint ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_devices:
        return _print_devices()
    if args.text is not None:
        return run_text_mode(args.text)
    return run_voice_mode(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
