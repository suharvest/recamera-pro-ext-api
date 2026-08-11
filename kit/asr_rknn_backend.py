"""
NPU (rv1126b) SenseVoice w4a16 ASR backend -- voxedge ASRBackend subclass.

Phase-2(a) of the voxedge-consumer refactor (see kit/asr.py). This is a
LIGHTWEIGHT voxedge `ASRBackend` implementation that reuses our already-verified
on-device decode:

    kaldi_native_fbank (80-bin, hamming)  ->  LFR (m=7,n=6, 560-dim)  ->  CMVN
    -> 4 SenseVoice prompt frames (lang / event / itn embeddings)
    -> RKNNLite encoder (w4a16, single-core init_runtime -- NO core_mask)
    -> greedy CTC collapse -> sentencepiece detokenize

Why NOT voxedge's stock `RKASRBackend` (voxedge.backends.rk.asr): that adapter
wraps the full `rkvoice_stream` stack (rknn-toolkit-lite2 + spm + kaldi_fbank +
rkvoice_stream, ~50-100 MB, and its `sensevoice_rknn.py` hard-codes
`core_mask=NPU_CORE_0` which is a 3576/3588 multi-core concept that ERRORS on the
single-core rv1126b). On a 2 GB device we instead implement the voxedge
`ASRBackend` interface directly over the exact decode we already proved works.
The backend is fully swappable via `Asr(backend="rk")` and satisfies the same
`transcribe_array(float32_16k) -> TranscriptionResult` contract as the CPU path,
so downstream (VAD / wake / state machine / app) is untouched.

Ported verbatim (numerically) from the device spike scripts
    /userdata/tmp/asr/device_e2e_rv.py
    /userdata/tmp/asr/device_decode_lowmem.py

Runtime deps (device venv /userdata/rknnenv): rknn-toolkit-lite2 (rknnlite),
kaldi_native_fbank, sentencepiece, numpy. All imported LAZILY in `preload()` so
this module imports on a Mac / CPU host without the NPU runtime.

Model + assets (staged on device):
    sensevoice_rv1126b_w4a16.rknn   the w4a16 encoder (127 MB)
    am.mvn                          CMVN stats (two 560-dim vectors)
    embedding.npy                   SenseVoice prompt embeddings
    chn_jpn_yue_eng_ko_spectok.bpe.model   sentencepiece model
"""
from __future__ import annotations

import glob
import io
import logging
import os
import re
import time
import wave
from typing import Optional

import numpy as np

from voxedge.backends.base import (
    ASRBackend,
    ASRCapability,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

# Decode constants -- identical to the verified device scripts.
# Dual-tier windows: a VAD segment whose LFR frame count (incl. 4 prompt frames)
# fits in T_SHORT is routed to the small T=100 encoder (~350ms); anything longer
# uses the production T=344 encoder (~1000ms). Both are w4a16, single-input, with
# the pad-mask baked as a constant, so the exact same decode applies to either.
T_SHORT = 100
T_LONG = 344
T_FIXED = T_LONG  # back-compat alias (>T_LONG segments are still truncated to T_LONG)
LFR_DIM = 560
BLANK_ID = 0
_LANG_IDS = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12}
_TEXTNORM_IDS = {"withitn": 14, "woitn": 15}

# Default on-device artifact filenames (co-located with the rknn model).
DEFAULT_RKNN_NAME = "sensevoice_rv1126b_w4a16.rknn"          # T=344 (production)
DEFAULT_RKNN_SHORT_NAME = "sensevoice_rv1126b_w4a16_t100.rknn"  # T=100 (short tier)
DEFAULT_CMVN_NAME = "am.mvn"
DEFAULT_EMB_NAME = "embedding.npy"
DEFAULT_BPE_NAME = "chn_jpn_yue_eng_ko_spectok.bpe.model"


# ── frontend (fbank + LFR + CMVN + prompt) ───────────────────────────────────
def _compute_feats(audio: np.ndarray) -> np.ndarray:
    import kaldi_native_fbank as knf
    o = knf.FbankOptions()
    o.frame_opts.samp_freq = 16000
    o.frame_opts.dither = 0.0
    o.frame_opts.window_type = "hamming"
    o.frame_opts.snip_edges = True
    o.mel_opts.num_bins = 80
    fb = knf.OnlineFbank(o)
    fb.accept_waveform(16000, (audio * 32768).tolist())
    fb.input_finished()
    return np.stack([fb.get_frame(i) for i in range(fb.num_frames_ready)])


def _apply_lfr(feats: np.ndarray, m: int = 7, n: int = 6) -> np.ndarray:
    T = feats.shape[0]
    pad = (m - 1) // 2
    feats = np.vstack([np.tile(feats[0], (pad, 1)), feats])
    T2 = feats.shape[0]
    out = []
    i = 0
    while i * n < T:
        idx0 = i * n
        if idx0 + m <= T2:
            out.append(feats[idx0:idx0 + m].reshape(-1))
        else:
            chunk = feats[idx0:T2]
            need = m - chunk.shape[0]
            chunk = np.vstack([chunk, np.tile(feats[-1], (need, 1))])
            out.append(chunk.reshape(-1))
        i += 1
    return np.stack(out).astype(np.float32)


def _load_cmvn(path: str) -> tuple[np.ndarray, np.ndarray]:
    txt = open(path).read()
    vals = [np.array(b.split(), dtype=np.float32)
            for b in re.findall(r"\[([^\]]*)\]", txt)]
    big = [v for v in vals if v.size == LFR_DIM]
    return big[0], big[1]


class RknnSenseVoiceBackend(ASRBackend):
    """voxedge `ASRBackend` over the rv1126b w4a16 SenseVoice RKNN encoder.

    Offline backend that opts into ``supports_offline_streaming`` so it gets the
    generic voxedge offline->streaming adapter + STREAMING capability for free,
    exactly like the CPU SenseVoice path.
    """

    supports_offline_streaming = True
    supports_hot_reload = True

    def __init__(
        self,
        rknn_model: str,
        cmvn_path: str,
        embedding_path: str,
        bpe_path: str,
        *,
        rknn_model_short: Optional[str] = None,
        language: str = "auto",
        textnorm: str = "withitn",
        debug: bool = False,
    ):
        self._rknn_model = rknn_model              # T=344 (long / production)
        self._rknn_model_short = rknn_model_short  # T=100 (short) -- optional
        self._cmvn_path = cmvn_path
        self._embedding_path = embedding_path
        self._bpe_path = bpe_path
        self._language = (language or "auto")
        self._textnorm = textnorm
        self._debug = bool(debug)
        # populated in preload()
        self._rknn = None        # long (T=344) handle
        self._rknn_short = None   # short (T=100) handle, or None -> single-tier
        self._sp = None
        self._cmvn_add = None
        self._cmvn_scale = None
        self._emb = None

    # -- voxedge ASRBackend interface ---------------------------------------- #
    @property
    def name(self) -> str:
        return "rk:sensevoice_w4a16"

    @property
    def capabilities(self) -> set:
        caps = set()
        if self._rknn is not None:
            caps.add(ASRCapability.OFFLINE)
            caps.add(ASRCapability.MULTI_LANGUAGE)
        return caps

    @property
    def sample_rate(self) -> int:
        return 16000

    def is_ready(self) -> bool:
        return self._rknn is not None and self._sp is not None

    def preload(self) -> None:
        """Load CMVN / embeddings / sentencepiece + init the NPU runtime once."""
        from rknnlite.api import RKNNLite
        import sentencepiece as spm

        self._cmvn_add, self._cmvn_scale = _load_cmvn(self._cmvn_path)
        self._emb = np.load(self._embedding_path)

        def _load_one(path: str) -> "RKNNLite":
            r = RKNNLite(verbose=False)
            if r.load_rknn(path) != 0:
                raise RuntimeError(f"load_rknn failed: {path}")
            # rv1126b is SINGLE-CORE: init_runtime() takes NO core_mask (the
            # NPU_CORE_0 mask used on rk3576/3588 errors here).
            if r.init_runtime() != 0:
                raise RuntimeError(f"init_runtime failed (rv1126b: no core_mask): {path}")
            return r

        t0 = time.time()
        self._rknn = _load_one(self._rknn_model)           # T=344 (always)
        # T=100 short tier -- optional; if it fails we degrade to single-tier.
        if self._rknn_model_short and os.path.exists(self._rknn_model_short):
            try:
                self._rknn_short = _load_one(self._rknn_model_short)
            except Exception:
                logger.exception("short-tier T=%d load failed; using long tier only",
                                 T_SHORT)
                self._rknn_short = None
        load_sec = time.time() - t0

        sp = spm.SentencePieceProcessor()
        sp.load(self._bpe_path)
        self._sp = sp
        logger.info("RknnSenseVoiceBackend loaded (%.2fs) long=%s short=%s",
                    load_sec, os.path.basename(self._rknn_model),
                    os.path.basename(self._rknn_model_short) if self._rknn_short else "(none)")

    def unload(self) -> None:
        for attr in ("_rknn", "_rknn_short"):
            r = getattr(self, attr, None)
            if r is not None:
                try:
                    r.release()
                except Exception:
                    logger.exception("RKNNLite.release failed (%s); continuing", attr)
                setattr(self, attr, None)
        self._sp = None
        import gc
        gc.collect()

    def transcribe(self, audio_bytes: bytes, language: str = "auto") -> TranscriptionResult:
        """One-shot offline transcription of WAV bytes (satisfies the ABC)."""
        audio, sr = self._wav_bytes_to_float(audio_bytes)
        if sr != 16000:
            audio = self._resample_16k(audio, sr)
        return self.transcribe_array(audio, language)

    def transcribe_array(self, samples: np.ndarray, language: str = "auto") -> TranscriptionResult:
        if self._rknn is None or self._sp is None:
            raise RuntimeError("RknnSenseVoiceBackend not loaded; call preload() first")
        lang = self._resolve_lang(language)
        audio = np.ascontiguousarray(samples, dtype=np.float32)

        # Front-end once (prompt + LFR + CMVN), then route by actual frame count.
        sp_in = self._prep(audio, lang)
        n = int(sp_in.shape[0])
        if self._rknn_short is not None and n <= T_SHORT:
            rk, T, tier = self._rknn_short, T_SHORT, "short"
        else:
            rk, T, tier = self._rknn, T_LONG, "long"

        speech, valid = self._pad(sp_in, T)
        out = rk.inference(inputs=[speech.astype(np.float32)])
        logits = np.asarray(out[0][0])[:valid]

        text, detected = self._ctc_decode(logits)
        logger.info("ASR route: tier=%s frames=%d T=%d valid=%d text=%r",
                    tier, n, T, valid, text[:48])
        return TranscriptionResult(text=text, language=detected or (lang if lang != "auto" else ""))

    # -- decode internals ---------------------------------------------------- #
    def _resolve_lang(self, requested: str) -> str:
        requested = (requested or "auto").strip().lower() or "auto"
        if requested in _LANG_IDS:
            return requested
        # Unknown/unsupported per-request language -> fall back to configured pin.
        return self._language if self._language in _LANG_IDS else "auto"

    def _prep(self, audio: np.ndarray, lang: str) -> np.ndarray:
        """Prompt frames + LFR + CMVN -> unpadded [N, LFR_DIM] feature sequence."""
        lfr = _apply_lfr(_compute_feats(audio))
        lfr = (lfr + self._cmvn_add) * self._cmvn_scale
        prefix = np.stack([
            self._emb[_LANG_IDS.get(lang, 0)],
            self._emb[1],
            self._emb[2],
            self._emb[_TEXTNORM_IDS[self._textnorm]],
        ]).astype(np.float32)
        return np.concatenate([prefix, lfr], axis=0).astype(np.float32)

    @staticmethod
    def _pad(sp_in: np.ndarray, T: int) -> tuple[np.ndarray, int]:
        """Pad/truncate the [N, LFR_DIM] sequence to exactly T frames for encoder T."""
        valid = int(sp_in.shape[0])
        if valid > T:
            sp_in = sp_in[:T]
            valid = T
        else:
            sp_in = np.vstack([sp_in, np.zeros((T - valid, LFR_DIM), dtype=np.float32)])
        return sp_in[None], valid

    def _ctc_decode(self, logits: np.ndarray) -> tuple[str, str]:
        ids = logits.argmax(-1).tolist()
        collapsed = []
        prev = -1
        for x in ids:
            if x != prev and x != BLANK_ID:
                collapsed.append(x)
            prev = x
        sp = self._sp
        pieces = [sp.id_to_piece(i) for i in collapsed if 0 <= i < sp.get_piece_size()]
        raw = "".join(pieces).replace("▁", " ")
        # Detected language rides in a <|xx|> tag -- but SenseVoice also emits
        # emotion / event / itn tags in the same <|..|> form, so match ONLY the
        # known language codes (else we'd report e.g. "withitn").
        m = re.search(r"<\|(zh|en|yue|ja|ko|nospeech)\|>", raw)
        detected = m.group(1) if m else ""
        text = re.sub(r"<\|[^|]*\|>", "", raw).strip()
        return text, detected

    # -- audio helpers ------------------------------------------------------- #
    @staticmethod
    def _wav_bytes_to_float(wav_bytes: bytes) -> tuple[np.ndarray, int]:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            sr, n, ch = wf.getframerate(), wf.getnframes(), wf.getnchannels()
            raw = wf.readframes(n)
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        return a, sr

    @staticmethod
    def _resample_16k(audio: np.ndarray, sr: int) -> np.ndarray:
        if sr == 16000:
            return audio
        new_len = int(len(audio) * 16000 / sr)
        return np.interp(
            np.linspace(0, len(audio) - 1, new_len),
            np.arange(len(audio)), audio,
        ).astype(np.float32)


def _resolve_assets(model: Optional[str]) -> dict:
    """Resolve the rknn model + co-located assets from a dir / model path.

    ``model`` may be: a directory, a path to the ``.rknn`` file, or any path
    whose *directory* holds the assets (e.g. the CPU ``model.int8.onnx`` the
    generic app config points at). Falls back to the standard staging dirs.
    """
    candidates = []
    if model:
        candidates.append(model if os.path.isdir(model) else os.path.dirname(model))
    candidates += ["/userdata/local/models/asr", "/userdata/tmp/asr"]

    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        rk = os.path.join(d, DEFAULT_RKNN_NAME)
        if not os.path.exists(rk):
            hits = sorted(glob.glob(os.path.join(d, "*w4a16*.rknn"))) \
                or sorted(glob.glob(os.path.join(d, "sensevoice_rv1126b*.rknn")))
            rk = hits[0] if hits else rk
        cmvn = os.path.join(d, DEFAULT_CMVN_NAME)
        emb = os.path.join(d, DEFAULT_EMB_NAME)
        bpe = os.path.join(d, DEFAULT_BPE_NAME)
        if not os.path.exists(bpe):
            hits = sorted(glob.glob(os.path.join(d, "*.bpe.model")))
            bpe = hits[0] if hits else bpe
        if os.path.exists(rk) and os.path.exists(cmvn) and os.path.exists(emb) \
                and os.path.exists(bpe):
            short = os.path.join(d, DEFAULT_RKNN_SHORT_NAME)
            return {"rknn_model": rk, "cmvn_path": cmvn,
                    "embedding_path": emb, "bpe_path": bpe,
                    "rknn_model_short": short if os.path.exists(short) else None}

    # Nothing complete found: return best-guess paths from the first candidate so
    # the error names the intended location.
    d = next((c for c in candidates if c), "/userdata/local/models/asr")
    short = os.path.join(d, DEFAULT_RKNN_SHORT_NAME)
    return {"rknn_model": os.path.join(d, DEFAULT_RKNN_NAME),
            "cmvn_path": os.path.join(d, DEFAULT_CMVN_NAME),
            "embedding_path": os.path.join(d, DEFAULT_EMB_NAME),
            "bpe_path": os.path.join(d, DEFAULT_BPE_NAME),
            "rknn_model_short": short if os.path.exists(short) else None}


def build_rknn_backend(model: Optional[str] = None, tokens: Optional[str] = None,
                       *, language: str = "auto", debug: bool = False,
                       **_kw) -> RknnSenseVoiceBackend:
    """Construct + preload the NPU backend. Called by ``kit.asr.Asr(backend='rk')``.

    ``tokens`` (the CPU sherpa tokens path) is ignored -- the NPU decode uses
    the sentencepiece bpe model resolved alongside the rknn model.
    """
    assets = _resolve_assets(model)
    b = RknnSenseVoiceBackend(language=language, debug=debug, **assets)
    b.preload()
    return b
