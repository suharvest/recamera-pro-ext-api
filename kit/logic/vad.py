"""
Voice Activity Detection (endpointing) for reCamera Pro voice apps.

P2 of the voice pipeline (docs/guide/voice-app.md §2/§3). Wraps sherpa-onnx's
built-in **silero VAD** (`sherpa_onnx.VoiceActivityDetector`) behind a small,
app-facing interface so the state machine (`kit.logic.voice_sm`) only ever sees
"a stream of `PcmFrame` in, complete `SpeechSegment`s out". Its job is
endpointing: turn a continuous 16 kHz mono PCM stream into utterance segments,
cutting on trailing silence (`min_silence_duration`) or a hard cap
(`max_speech_duration`).

Model artifact (staged on device in the SHARED model dir, reuse verbatim):
    /userdata/local/models/asr/silero_vad.onnx   silero VAD v5 ONNX (~2.2 MB)

sherpa-onnx VAD contract (verified on device, sherpa_onnx 1.13.4, 2026-08-09):
    VoiceActivityDetector.accept_waveform(float32[])   # feed any-length chunk
    .empty()/.front/.pop()                             # drain finished segments
    .is_speech_detected()                              # live "in speech" flag
    .flush()                                           # force-close last segment
    front -> SpeechSegment{ samples: float32[], start: int (sample index) }

sherpa is imported lazily in __init__ so importing this module for type hints on
a host without sherpa does not fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Union

DEFAULT_VAD_MODEL = "/userdata/local/models/asr/silero_vad.onnx"


@dataclass
class SpeechSegment:
    """One endpointed utterance emitted by the VAD.

    `pcm` is little-endian int16 16 kHz mono bytes -- exactly what `Asr.transcribe`
    and `PcmFrame.pcm` expect, so a segment feeds straight into transcription.
    """
    pcm: bytes
    start_sec: float
    duration_sec: float


def _to_float32(pcm, ch: int = 1):
    """Coerce PcmFrame / int16 bytes / ndarray -> mono float32 [-1, 1] for sherpa."""
    import numpy as np
    # duck-type PcmFrame (avoid import cycle with adapters)
    raw = getattr(pcm, "pcm", pcm)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(bytes(raw), dtype=np.int16).astype(np.float32) / 32768.0
    else:
        arr = np.asarray(raw)
        arr = (arr.astype(np.float32) / 32768.0) if arr.dtype == np.int16 else arr.astype(np.float32)
    if arr.ndim > 1:
        arr = arr.reshape(arr.shape[0], -1).mean(axis=1)
    return np.ascontiguousarray(arr, dtype=np.float32)


class VadSegmenter:
    """Silero-VAD endpointer: PcmFrame stream in -> SpeechSegment stream out.

    Typical use (driven by the state machine)::

        vad = VadSegmenter()
        for frame in audio_source:
            vad.accept(frame)
            for seg in vad.segments():      # 0..n finished utterances
                text = asr.transcribe(seg.pcm).text
        vad.flush()                          # at stream end, close trailing speech
        for seg in vad.segments():
            ...
    """

    def __init__(
        self,
        model: str = DEFAULT_VAD_MODEL,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_duration: float = 0.6,
        min_speech_duration: float = 0.25,
        max_speech_duration: float = 15.0,
        window_size: int = 512,
        num_threads: int = 1,
        buffer_seconds: float = 30.0,
        preroll_ms: float = 300.0,
    ):
        import sherpa_onnx  # lazy: only the VAD path needs it
        self.sample_rate = int(sample_rate)
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = model
        cfg.silero_vad.threshold = float(threshold)
        cfg.silero_vad.min_silence_duration = float(min_silence_duration)
        cfg.silero_vad.min_speech_duration = float(min_speech_duration)
        cfg.silero_vad.max_speech_duration = float(max_speech_duration)
        cfg.silero_vad.window_size = int(window_size)
        cfg.sample_rate = self.sample_rate
        cfg.num_threads = int(num_threads)
        self._vad = sherpa_onnx.VoiceActivityDetector(
            cfg, buffer_size_in_seconds=float(buffer_seconds))

        # -- pre-roll look-back (fixes clipped utterance heads) --------------- #
        # sherpa's silero VAD only starts a segment once it has *confirmed*
        # speech (needs `min_speech_duration` of voiced audio + model warm-up),
        # so the first ~0.2-0.3 s of a word is dropped ("今天" -> "天天"). We keep
        # a rolling buffer of the most-recent samples we fed and, when a segment
        # is emitted, prepend the `preroll_ms` that sit immediately *before* the
        # VAD's reported start index. `seg.start` is an absolute sample offset
        # into the same stream we buffer, so the splice is exact -- the pre-roll
        # is strictly [start-preroll, start) and never overlaps seg.samples.
        self.preroll_samples = max(0, int(self.sample_rate * float(preroll_ms) / 1000.0))
        # Retain enough history to reach back to (start - preroll) at emission
        # time: a segment is emitted up to max_speech + min_silence after it
        # started, so the look-back must span that plus the preroll (+1 s slack).
        self._ring_cap = self.preroll_samples + int(
            self.sample_rate * (float(max_speech_duration)
                                + float(min_silence_duration) + 1.0))
        self._fed = 0            # total samples fed to the VAD since last reset
        self._ring = None        # np.float32 tail of the fed stream (<= _ring_cap)

    def accept(self, pcm: Union[bytes, "object"]) -> None:
        """Feed one chunk (PcmFrame / int16 bytes / ndarray). Any length is fine."""
        import numpy as np
        arr = _to_float32(pcm)
        self._vad.accept_waveform(arr)
        # mirror the exact same samples into the rolling look-back buffer so its
        # indexing stays aligned with sherpa's `seg.start`.
        self._fed += arr.size
        if self.preroll_samples:
            self._ring = arr.copy() if self._ring is None \
                else np.concatenate([self._ring, arr])
            if self._ring.size > self._ring_cap:
                self._ring = self._ring[-self._ring_cap:]

    def _preroll_before(self, start: int):
        """Return the float32 samples in [start-preroll, start) still buffered."""
        import numpy as np
        if not self.preroll_samples or self._ring is None:
            return np.empty(0, dtype=np.float32)
        base = self._fed - self._ring.size          # abs index of self._ring[0]
        lo = max(base, start - self.preroll_samples)
        if start <= lo:
            return np.empty(0, dtype=np.float32)
        return self._ring[lo - base: start - base]

    def segments(self) -> Iterator[SpeechSegment]:
        """Yield every finished utterance currently buffered, oldest first."""
        import numpy as np
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            pre = self._preroll_before(int(seg.start))
            if pre.size:
                samples = np.concatenate([pre, samples])
            pcm = (np.clip(samples * 32768.0, -32768, 32767)
                   .astype(np.int16).tobytes())
            self._vad.pop()
            yield SpeechSegment(
                pcm=pcm,
                start_sec=(int(seg.start) - pre.size) / self.sample_rate,
                duration_sec=samples.size / self.sample_rate,
            )

    def is_speech(self) -> bool:
        """True while the model currently believes speech is ongoing."""
        return bool(self._vad.is_speech_detected())

    def flush(self) -> None:
        """Force-close any in-progress speech (call at end-of-stream)."""
        self._vad.flush()

    def reset(self) -> None:
        """Drop all state/buffered segments (call when entering a fresh listen)."""
        self._vad.reset()
        self._fed = 0
        self._ring = None
