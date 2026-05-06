"""Issue 3 – Captura de Audio Local com Segmentacao de Fala."""
import array
import struct
import threading
import time
from typing import Iterator

import pytest

from assistente_voz.audio_capture import (
    AmplitudeVAD,
    AudioCapture,
    AudioSegment,
    AudioSource,
    CaptureState,
    VADPolicy,
)


# ── test doubles ──────────────────────────────────────────────────────────────


def _pcm_chunk(amplitude: int, n_samples: int = 1024) -> bytes:
    """Build a 16-bit PCM chunk with constant amplitude."""
    samples = array.array("h", [amplitude] * n_samples)
    return samples.tobytes()


LOUD = _pcm_chunk(20_000)   # above AmplitudeVAD default threshold
SILENT = _pcm_chunk(0)      # silence


class FixedSource(AudioSource):
    """Deterministic source that plays a predefined sequence of chunks."""

    sample_rate = 16000
    channels = 1
    chunk_frames = 1024

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._index = 0

    def open(self) -> None:
        self._index = 0

    def close(self) -> None:
        pass

    def read_chunk(self) -> bytes:
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def source_name(self) -> str:
        return "test"


class AlwaysSpeechVAD(VADPolicy):
    def is_speech(self, chunk: bytes, sample_rate: int) -> bool:
        return True


class NeverSpeechVAD(VADPolicy):
    def is_speech(self, chunk: bytes, sample_rate: int) -> bool:
        return False


class BlockingSource(AudioSource):
    """Source that blocks read_chunk until released via stop_event."""

    sample_rate = 16000
    channels = 1
    chunk_frames = 1024

    def __init__(self) -> None:
        self._stop = threading.Event()

    def open(self) -> None:
        self._stop.clear()

    def close(self) -> None:
        pass

    def read_chunk(self) -> bytes:
        # Block until released, then signal end-of-stream
        self._stop.wait()
        return b""

    def release(self) -> None:
        self._stop.set()

    def source_name(self) -> str:
        return "test"


# ── helpers ───────────────────────────────────────────────────────────────────


def _collect_segments(capture: AudioCapture, timeout: float = 2.0) -> list[AudioSegment]:
    segments: list[AudioSegment] = []
    done = threading.Event()

    def on_seg(seg: AudioSegment) -> None:
        segments.append(seg)

    capture.start(on_seg)
    # wait until thread finishes (source exhausted) or timeout
    assert capture._thread is not None
    capture._thread.join(timeout=timeout)
    capture.stop()
    return segments


# ── tests ─────────────────────────────────────────────────────────────────────


def test_start_changes_state_to_listening():
    source = BlockingSource()
    cap = AudioCapture(source=source, vad=NeverSpeechVAD())
    cap.start(lambda seg: None)
    assert cap.state == CaptureState.LISTENING
    source.release()
    cap.stop()


def test_stop_sets_state_to_idle():
    source = FixedSource([SILENT])
    cap = AudioCapture(source=source, vad=NeverSpeechVAD())
    cap.start(lambda seg: None)
    cap.stop()
    assert cap.state == CaptureState.IDLE


def test_stop_does_not_leave_thread_running():
    source = FixedSource([SILENT])
    cap = AudioCapture(source=source, vad=NeverSpeechVAD())
    cap.start(lambda seg: None)
    cap.stop()
    assert cap._thread is not None
    assert not cap._thread.is_alive()


def test_speech_segment_emitted_with_valid_metadata():
    # speech chunk followed by enough silence to trigger flush
    chunks = [LOUD] * 3 + [SILENT] * 12
    source = FixedSource(chunks)
    cap = AudioCapture(source=source, vad=AmplitudeVAD(threshold=100), silence_chunks_to_flush=10)

    segments = _collect_segments(cap)

    assert len(segments) >= 1
    seg = segments[0]
    assert seg.source == "test"
    assert seg.sample_rate == 16000
    assert seg.channels == 1
    assert isinstance(seg.audio_bytes, bytes)
    assert len(seg.audio_bytes) > 0
    assert isinstance(seg.timestamp, float)


def test_no_segment_emitted_when_only_silence():
    source = FixedSource([SILENT] * 5)
    cap = AudioCapture(source=source, vad=NeverSpeechVAD())
    segments = _collect_segments(cap)
    assert segments == []


def test_remaining_speech_flushed_on_stop():
    """Speech buffer should be emitted when the source ends mid-speech."""
    chunks = [LOUD] * 5  # speech with no trailing silence
    source = FixedSource(chunks)
    cap = AudioCapture(source=source, vad=AlwaysSpeechVAD(), silence_chunks_to_flush=10)
    segments = _collect_segments(cap)
    assert len(segments) == 1
    assert len(segments[0].audio_bytes) > 0


def test_double_start_raises():
    source = BlockingSource()
    cap = AudioCapture(source=source, vad=NeverSpeechVAD())
    cap.start(lambda seg: None)
    with pytest.raises(RuntimeError):
        cap.start(lambda seg: None)
    source.release()
    cap.stop()
