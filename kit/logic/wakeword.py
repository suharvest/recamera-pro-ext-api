"""
Wake-word / keyword-spotting for reCamera Pro voice apps.

P2 of the voice pipeline (docs/guide/voice-app.md §2/§3). Sits in front of the
state machine's `idle` state: continuously consumes the `PcmFrame` stream and
fires a `WakeEvent` when the configured wake word is heard. Hidden behind the
`WakeWord` ABC so the state machine never cares which backend detected the wake
-- when the official R8 firmware exposes RK's built-in AAD/wakeup, a new backend
drops in here and nothing downstream changes.

Two interchangeable backends ship here:

1. `SherpaKwsWakeWord` -- PREFERRED. sherpa-onnx `KeywordSpotter` (a tiny
   streaming zipformer transducer). Always-listening, low CPU, no transcription
   in the idle loop. Needs a KWS model (encoder/decoder/joiner + tokens) and a
   `keywords_file`. We ship the gigaspeech 3.3M English KWS model and a custom
   keyword "HELLO CAMERA" (BPE: `▁HE LL O ▁CAME RA`).
   Model artifacts (staged on device, reuse verbatim):
       /userdata/tmp/asr/kws/encoder.int8.onnx   (~4.6 MB)
       /userdata/tmp/asr/kws/decoder.int8.onnx
       /userdata/tmp/asr/kws/joiner.int8.onnx
       /userdata/tmp/asr/kws/tokens.txt
       /userdata/tmp/asr/kws/keywords.txt

2. `AsrKeywordWakeWord` -- FALLBACK, zero extra models. Runs the already-proven
   VAD + SenseVoice ASR: endpoint a short utterance, transcribe it, and wake if
   the configured wake phrase is a substring of the transcript. Slower/heavier
   in idle (it transcribes every utterance) but rock-solid and trivially
   reconfigurable to any phrase / language ("你好小西", "hey camera", ...).

sherpa-onnx KeywordSpotter contract (verified on device, 1.13.4, 2026-08-09):
    KeywordSpotter(tokens, encoder, decoder, joiner, keywords_file, ...)
    .create_stream() -> stream
    stream.accept_waveform(sample_rate, float32[])
    .is_ready(stream) / .decode_stream(stream)
    .get_result(stream) -> str  (non-empty == a keyword just fired)
    .reset_stream(stream)       (call after a hit to re-arm)
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Union

from kit.logic.vad import VadSegmenter, _to_float32

DEFAULT_KWS_DIR = "/userdata/tmp/asr/kws"


@dataclass
class WakeEvent:
    """Emitted the moment a wake word is detected."""
    keyword: str            # the matched wake phrase
    backend: str            # "sherpa-kws" | "asr-keyword"
    score: float = 1.0
    transcript: str = ""    # (asr-keyword) the full utterance the match came from


def _norm(s: str) -> str:
    """Lowercase + strip everything but alnum/CJK, for language-agnostic match."""
    return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").lower())


class WakeWord(ABC):
    """Abstract wake-word detector. Fed the raw PcmFrame stream in `idle`."""

    @abstractmethod
    def accept(self, frame: Union[bytes, "object"]) -> Optional[WakeEvent]:
        """Consume one chunk; return a WakeEvent iff the wake word just fired."""
        raise NotImplementedError

    def reset(self) -> None:  # optional override
        """Re-arm / drop partial state (state machine calls on entering idle)."""


# --- Preferred backend: sherpa-onnx streaming KeywordSpotter ----------------- #
class SherpaKwsWakeWord(WakeWord):
    """Always-listening KWS via a small streaming transducer. No transcription."""

    def __init__(
        self,
        keywords_file: str = f"{DEFAULT_KWS_DIR}/keywords.txt",
        *,
        tokens: str = f"{DEFAULT_KWS_DIR}/tokens.txt",
        encoder: str = f"{DEFAULT_KWS_DIR}/encoder.int8.onnx",
        decoder: str = f"{DEFAULT_KWS_DIR}/decoder.int8.onnx",
        joiner: str = f"{DEFAULT_KWS_DIR}/joiner.int8.onnx",
        num_threads: int = 1,
        sample_rate: int = 16000,
        keywords_score: float = 1.5,
        keywords_threshold: float = 0.25,
        provider: str = "cpu",
    ):
        import sherpa_onnx  # lazy
        self.sample_rate = int(sample_rate)
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=tokens, encoder=encoder, decoder=decoder, joiner=joiner,
            keywords_file=keywords_file, num_threads=int(num_threads),
            keywords_score=float(keywords_score),
            keywords_threshold=float(keywords_threshold),
            provider=provider,
        )
        self._stream = self._spotter.create_stream()

    def accept(self, frame: Union[bytes, "object"]) -> Optional[WakeEvent]:
        samples = _to_float32(frame)
        if samples.size == 0:
            return None
        self._stream.accept_waveform(self.sample_rate, samples)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        hit = self._spotter.get_result(self._stream)
        if hit:
            # re-arm so the same keyword can fire again next time
            self._spotter.reset_stream(self._stream)
            return WakeEvent(keyword=hit, backend="sherpa-kws")
        return None

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)


# --- Fallback backend: VAD-segment + ASR substring match --------------------- #
class AsrKeywordWakeWord(WakeWord):
    """Wake when an endpointed utterance's transcript contains the wake phrase.

    Reuses the proven VAD + ASR stack, so it needs no extra model and works for
    any phrase/language out of the box. `asr` is a `kit.asr.Asr`; `vad` is an
    owned `VadSegmenter` (separate from the state machine's listening VAD).
    """

    def __init__(
        self,
        asr,
        keywords: Union[str, List[str]],
        *,
        vad: Optional[VadSegmenter] = None,
        vad_kwargs: Optional[dict] = None,
    ):
        self.asr = asr
        self.keywords = [keywords] if isinstance(keywords, str) else list(keywords)
        self._norm_keywords = [_norm(k) for k in self.keywords]
        self.vad = vad if vad is not None else VadSegmenter(**(vad_kwargs or {}))

    def accept(self, frame: Union[bytes, "object"]) -> Optional[WakeEvent]:
        self.vad.accept(frame)
        for seg in self.vad.segments():
            text = self.asr.transcribe(seg.pcm).text
            norm = _norm(text)
            for kw, nkw in zip(self.keywords, self._norm_keywords):
                if nkw and nkw in norm:
                    return WakeEvent(keyword=kw, backend="asr-keyword",
                                     transcript=text)
        return None

    def reset(self) -> None:
        self.vad.reset()
