#!/usr/bin/env python3
"""
voice-transcribe -- reCamera Pro voice app (wake word -> VAD -> ASR).

This is the P3 packaging of the verified voice pipeline. Unlike the vision apps
it does NOT use the FrameSource / RknnModel base loop -- it overrides `run()` to
drive the audio pipeline instead:

    RtspAudioSource (mic via rkipc RTSP audio track, no /dev/snd takeover)
        -> VoiceStateMachine (idle --wake--> listening --endpoint--> transcribing)
             WakeWord (sherpa KWS | ASR keyword)  +  VAD (silero)  +  Asr (SenseVoice)
        -> ResultSink.emit(events) -> /appcenter WS panel  (+ optional MQTT/HA)

It still subclasses `kit.app.App` so it plugs into the standard `run_app` CLI
(config load, WsResultSink on the manifest port, MQTT fan-out) unchanged. Because
`needs_model = False` the base loop never constructs an RknnModel, and the
`RknnModel` import in kit.app is lazy, so this runs cleanly under the sherpa venv
`/userdata/rknnenv/bin/python` (which has sherpa-onnx but no rknnlite/cv2).

Models live in a SHARED directory, NOT inside the app package (they are hundreds
of MB and shared across voice apps). Resolution order (first that exists wins):
    1. config `model_dir`            (default /userdata/local/models/asr)
    2. /userdata/local/models/asr    (the shared convention)
    3. /userdata/tmp/asr             (feasibility-spike staging; fallback)

Test injection: set env `RECAMERA_VOICE_WAV=/path/to.wav` (or config `wav_file`)
to feed a "<wake word> + <sentence>" WAV through `WavFileAudioSource` instead of
the live RTSP mic -- the whole state machine runs identically. `RECAMERA_VOICE_MAX_WAKES`
stops after N completed transcripts (used by the on-device e2e test).
"""
import os
import sys

# Make the shared Kit package importable whether launched by appmgr or by hand.
_here = os.path.dirname(os.path.abspath(__file__))
_kit_parent_env = os.environ.get("KIT_PARENT")
_kit_dir_env = os.environ.get("KIT_DIR")
for _cand in (
    _kit_parent_env,
    os.path.dirname(_kit_dir_env) if _kit_dir_env else None,
    os.path.join(_here, ".."),                       # device: /userdata/local/apps
    os.path.join(_here, "..", ".."),                 # repo: recamera_pro/
    "/userdata/local/kit",                           # device shared kit
    "/userdata/local/apps",                          # device fallback
):
    if _cand and os.path.isdir(os.path.join(_cand, "kit")):
        _cand = os.path.abspath(_cand)
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from kit.app import App, run_app          # noqa: E402


# States mirrored from kit.logic.voice_sm (kept local to avoid importing sherpa
# at module import time -- voice_sm only pulls sherpa when a pipeline is built).
IDLE = "idle"

SHARED_MODEL_DIR = "/userdata/local/models/asr"
STAGING_MODEL_DIR = "/userdata/tmp/asr"


class VoiceTranscribeApp(App):
    id = "voice-transcribe"
    name = "Voice Transcribe"
    postproc = "voice"
    needs_model = False          # no NPU model: base loop never builds RknnModel

    # -- config --------------------------------------------------------------- #
    def setup(self, config):
        super().setup(config or {})
        c = self.config
        self.wake_backend = str(c.get("wake_backend", "kws")).lower()
        # ASR backend selector (voxedge consumer): "rk" (NPU w4a16 via
        # kit.asr_rknn_backend, default -- the shared model dir ships the w4a16
        # .rknn, NOT a sherpa model.int8.onnx) or "sherpa" (CPU). Downstream is
        # identical. Mirrored as config_schema default so appmgr injects it and a
        # UI config-set never drops it.
        self.asr_backend = str(c.get("asr_backend", "rk")).lower()
        self.wakeword = str(c.get("wakeword", "hello camera"))
        self.language = str(c.get("language", "auto"))
        self.min_silence_sec = float(c.get("min_silence_sec", 0.6))
        self.max_utterance_sec = float(c.get("max_utterance_sec", 15.0))
        # Pre-roll look-back: prepend this much audio from *before* the VAD's
        # confirmed speech-start so clipped utterance heads ("今天"->"天天") are
        # recovered. Too large and the wake-word tail ("...CAMERA") leaks in.
        self.preroll_ms = float(c.get("preroll_ms", 300.0))
        self.listen_timeout_sec = float(c.get("listen_timeout_sec", 8.0))
        self.kws_threshold = float(c.get("kws_threshold", 0.25))
        self.kws_score = float(c.get("kws_score", 1.5))
        self.model_dir = self._resolve_model_dir(c.get("model_dir") or SHARED_MODEL_DIR)
        # runtime state broadcast to the panel / HA summary
        self._state = IDLE
        self._last_text = ""
        self._sink = None

    def _resolve_model_dir(self, preferred):
        """First existing dir among (config, shared convention, staging)."""
        for d in (preferred, SHARED_MODEL_DIR, STAGING_MODEL_DIR):
            if d and os.path.isdir(d):
                return d
        # nothing exists yet: return the shared convention so the error message
        # points at the intended location.
        return preferred or SHARED_MODEL_DIR

    # -- WS / MQTT event plumbing --------------------------------------------- #
    def _on_voice_event(self, ev):
        """VoiceStateMachine callback -> shape + publish one event to the sink.

        The state machine emits {"type": state|wake|transcript|listen_timeout, ...}.
        We mirror `type` into `kind` (the field the /appcenter event log + MQTT
        summary key off), and attach a top-level `state` + `summary{state,text}`
        so the panel and Home Assistant always see the current state and the last
        transcript regardless of which event arrived.
        """
        kind = ev.get("type", "event")
        out = dict(ev)
        out["kind"] = kind
        if kind == "state":
            self._state = ev.get("state", self._state)
        elif kind == "transcript":
            self._last_text = ev.get("text", "") or self._last_text
        payload = {
            "results": [],
            "events": [out],
            "state": self._state,
            "summary": {"state": self._state, "text": self._last_text},
        }
        if self._sink is not None:
            try:
                self._sink.emit(payload, float(ev.get("t", 0.0)))
            except Exception:
                pass

    # -- main loop (overrides the vision base loop entirely) ------------------ #
    def run(self, model_path=None, *, source="ffmpeg", url=None,
            sink=None, n=0, every=1, verbose=True, **_kw):
        from kit.asr import Asr
        from kit.logic.vad import VadSegmenter
        from kit.logic.wakeword import SherpaKwsWakeWord, AsrKeywordWakeWord
        from kit.logic.voice_sm import VoiceStateMachine

        self._sink = sink
        md = self.model_dir
        asr_model = os.path.join(md, "model.int8.onnx")
        asr_tokens = os.path.join(md, "tokens.txt")
        vad_model = os.path.join(md, "silero_vad.onnx")
        kws_dir = os.path.join(md, "kws")

        if verbose:
            print(f"[app:{self.id}] model_dir={md} backend={self.wake_backend} "
                  f"lang={self.language} min_silence={self.min_silence_sec} "
                  f"max_utt={self.max_utterance_sec} preroll_ms={self.preroll_ms} "
                  f"sink={type(sink).__name__}",
                  flush=True)

        # audio source: WAV injection (tests) or live RTSP mic (default) ------- #
        wav = os.environ.get("RECAMERA_VOICE_WAV") or self.config.get("wav_file")
        if wav:
            from kit.adapters.audio_source import WavFileAudioSource
            rt = str(os.environ.get("RECAMERA_VOICE_WAV_REALTIME", "")).strip().lower() \
                in ("1", "true", "yes", "on")
            if verbose:
                print(f"[app:{self.id}] audio source = WavFileAudioSource({wav}) "
                      f"realtime={rt}", flush=True)
            src = WavFileAudioSource(wav, realtime=rt, pad_silence_sec=1.0)
        else:
            from kit.adapters.audio_source import DEFAULT_AUDIO_FILTER
            # The live mic is very quiet (~-49 dBFS); apply an adaptive gain
            # filter so KWS/ASR get a normal level (wake-word detection depends
            # on it -- see DEFAULT_AUDIO_FILTER). Same knob on every backend:
            # "" / "none" disables -> unity gain.
            audio_filter = self.config.get("audio_filter", DEFAULT_AUDIO_FILTER)
            # audio_source: "ai_asr" (official, default) or "rtsp" (fallback).
            #   ai_asr -> ALSA shared-capture PCM: clean, no rkipc transcode hop,
            #             shares the mic with rkipc via dsnoop (no takeover). Needs
            #             root/audio group -> OK under appmgr (runs as root).
            #   rtsp   -> demux rkipc's combined RTSP audio track (kept as a
            #             fallback / A-B comparison; works even as non-root).
            audio_backend = str(self.config.get("audio_source", "ai_asr")).lower()
            if audio_backend == "rtsp":
                from kit.adapters.audio_source import RtspAudioSource
                rtsp = url or self.config.get("rtsp_url") \
                    or "rtsp://admin:admin@127.0.0.1:5554/live/1"
                if verbose:
                    print(f"[app:{self.id}] audio source = RtspAudioSource({rtsp}) "
                          f"audio_filter={audio_filter!r}", flush=True)
                src = RtspAudioSource(rtsp, audio_filter=audio_filter)
            else:
                from kit.adapters.audio_source import AiAsrAudioSource
                device = self.config.get("ai_asr_device", "ai_asr")
                if verbose:
                    print(f"[app:{self.id}] audio source = "
                          f"AiAsrAudioSource({device}) audio_filter="
                          f"{audio_filter!r}", flush=True)
                src = AiAsrAudioSource(device, audio_filter=audio_filter)

        if verbose:
            print(f"[app:{self.id}] loading SenseVoice ASR (int8)...", flush=True)
        asr = Asr(model=asr_model, tokens=asr_tokens, language=self.language,
                  backend=self.asr_backend)
        vad = VadSegmenter(model=vad_model,
                           min_silence_duration=self.min_silence_sec,
                           max_speech_duration=self.max_utterance_sec,
                           preroll_ms=self.preroll_ms)

        if self.wake_backend == "asr":
            if verbose:
                print(f"[app:{self.id}] wake backend = ASR keyword {self.wakeword!r}",
                      flush=True)
            # Give the wake-word's internal VAD the SAME model_dir silero as the
            # listening VAD above -- otherwise AsrKeywordWakeWord builds a bare
            # VadSegmenter() that falls back to kit.logic.vad.DEFAULT_VAD_MODEL.
            wake = AsrKeywordWakeWord(asr, self.wakeword.split("|"),
                                     vad_kwargs={"model": vad_model})
        else:
            if verbose:
                print(f"[app:{self.id}] wake backend = sherpa KeywordSpotter "
                      f"({kws_dir})", flush=True)
            wake = SherpaKwsWakeWord(
                keywords_file=os.path.join(kws_dir, "keywords.txt"),
                tokens=os.path.join(kws_dir, "tokens.txt"),
                encoder=os.path.join(kws_dir, "encoder.int8.onnx"),
                decoder=os.path.join(kws_dir, "decoder.int8.onnx"),
                joiner=os.path.join(kws_dir, "joiner.int8.onnx"),
                keywords_threshold=self.kws_threshold,
                keywords_score=self.kws_score,
            )

        sm = VoiceStateMachine(src, wake, vad, asr,
                               on_event=self._on_voice_event,
                               listen_timeout_sec=self.listen_timeout_sec,
                               verbose=verbose)
        # publish the initial idle state so the panel shows something immediately
        self._on_voice_event({"type": "state", "state": IDLE})

        max_wakes = int(os.environ.get("RECAMERA_VOICE_MAX_WAKES", "0") or 0)
        if verbose:
            print(f"[app:{self.id}] ready -- listening (max_wakes={max_wakes})",
                  flush=True)
        count = sm.run(max_wakes=max_wakes)
        if verbose:
            print(f"[app:{self.id}] stopped after {count} transcript(s)", flush=True)


if __name__ == "__main__":
    run_app(VoiceTranscribeApp())
