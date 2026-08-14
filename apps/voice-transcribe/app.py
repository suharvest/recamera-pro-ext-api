#!/usr/bin/env python3
"""
voice-transcribe -- reCamera Pro voice app (wake word -> VAD -> ASR).

This is the P3 packaging of the verified voice pipeline. It is the SELF-PACED
variant of the new app shape (internal/KIT_APP_SHAPE_SPEC.md §3, "接管"): the
input is audio chunks, not camera frames, so `run()` drives the pipeline at its
own rhythm instead of iterating `self.frames()`:

    AiAsrAudioSource / RtspAudioSource (mic; no /dev/snd takeover)
        -> VoiceStateMachine (idle --wake--> listening --endpoint--> transcribing)
             WakeWord (sherpa KWS | ASR keyword)  +  VAD (silero)  +  Asr (SenseVoice)
        -> self.emit(events, t) -> the manifest `output` block (WS panel, MQTT/HA)

Two class flags declare that shape to kit:

    owns_loop    = True   -- `def run(self):` takes over the rhythm
    needs_frames = False  -- kit opens NO camera frame source; this app never
                             touches /dev/video, the RTSP video stream or VPSS

Owning the rhythm costs nothing else (spec §2 hard constraint: "an escape hatch
must not strip the rest of the infrastructure"). kit still auto-binds every
`config_schema` key onto `self`, still re-binds the apply:"live" ones on SIGHUP
(`self.tick()` applies them at an event boundary and kit routes the same change
into the output sink), and `self.emit()` still publishes through the sink
assembled from the manifest `output` block. Consequently this file contains no
sink lookup, no output plumbing and no MQTT code at all.

Because `needs_model = False` kit never constructs an RknnModel, and the
`RknnModel` import in kit.app is lazy, so this runs cleanly under the sherpa venv
`/userdata/rknnenv/bin/python` (which has sherpa-onnx but no rknnlite).

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

from kit.app import App, run_app


# States mirrored from kit.logic.voice_sm (kept local to avoid importing sherpa
# at module import time -- voice_sm only pulls sherpa when a pipeline is built).
IDLE = "idle"

SHARED_MODEL_DIR = "/userdata/local/models/asr"
STAGING_MODEL_DIR = "/userdata/tmp/asr"


class VoiceTranscribeApp(App):
    id = "voice-transcribe"
    name = "Voice Transcribe"
    postproc = "voice"
    owns_loop = True             # explicit new shape: run() owns the rhythm
    needs_frames = False         # audio app: kit opens no camera frame source
    needs_model = False          # no NPU model: kit never builds an RknnModel

    # -- config --------------------------------------------------------------- #
    def setup(self, config):
        """Normalise the auto-bound `config_schema` params.

        kit has already bound every schema key onto `self` before this runs
        (App.start -> _bind_params), with the declared type applied. What is
        left here is the app-specific normalisation the schema cannot express:
        case-folding the enums and resolving `model_dir` against the shared
        model locations. `getattr(self, k, ...)` keeps a hand-built config that
        omits a key working (unit tests, `--sink stdout` by hand).
        """
        super().setup(config or {})
        c = self.config

        def _s(key, default):
            return str(getattr(self, key, None) or c.get(key, default))

        def _f(key, default):
            try:
                return float(getattr(self, key, c.get(key, default)))
            except (TypeError, ValueError):
                return float(default)

        self.wake_backend = _s("wake_backend", "kws").lower()
        # ASR backend selector (voxedge consumer): "rk" (NPU w4a16 via
        # kit.asr_rknn_backend, default -- the shared model dir ships the w4a16
        # .rknn, NOT a sherpa model.int8.onnx) or "sherpa" (CPU). Downstream is
        # identical.
        self.asr_backend = _s("asr_backend", "rk").lower()
        self.wakeword = _s("wakeword", "hello camera")
        self.language = _s("language", "auto")
        self.min_silence_sec = _f("min_silence_sec", 0.6)
        self.max_utterance_sec = _f("max_utterance_sec", 15.0)
        # Pre-roll look-back: prepend this much audio from *before* the VAD's
        # confirmed speech-start so clipped utterance heads ("今天"->"天天") are
        # recovered. Too large and the wake-word tail ("...CAMERA") leaks in.
        self.preroll_ms = _f("preroll_ms", 300.0)
        self.listen_timeout_sec = _f("listen_timeout_sec", 8.0)
        self.kws_threshold = _f("kws_threshold", 0.25)
        self.kws_score = _f("kws_score", 1.5)
        self.model_dir = self._resolve_model_dir(
            getattr(self, "model_dir", None) or c.get("model_dir")
            or SHARED_MODEL_DIR)
        # runtime state broadcast to the panel / HA summary
        self._state = IDLE
        self._last_text = ""

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- kit already re-bound the values; just log.

        The apply:"live" knobs are wakeword / min_silence_sec / max_utterance_sec
        / preroll_ms / listen_timeout_sec / audio_filter. kit's SIGHUP path
        re-binds each of them onto `self` (typed per the schema) and separately
        re-applies the output filters/template on the sink, so nothing is done
        by hand here. The apply:"restart" knobs (wake_backend, asr_backend,
        language, kws_*, model_dir, audio_source) never reach this hook, so the
        audio source / VAD / wake-word / state machine are never rebuilt.

        NOTE: this app runs its own blocking audio loop, so the currently-running
        VAD/wake/SM captured these values at construction; a value replaced here
        takes effect on the next start/restart, not mid-utterance. See TODO in
        the port notes re: wiring a live SIGHUP path into VoiceStateMachine.
        """
        print(f"[voice-transcribe] hot-reload changed={sorted(changed)} "
              f"wakeword={self.wakeword!r} min_silence={self.min_silence_sec} "
              f"max_utt={self.max_utterance_sec} preroll_ms={self.preroll_ms} "
              f"listen_timeout={self.listen_timeout_sec} "
              f"audio_filter={getattr(self, 'audio_filter', None)!r}", flush=True)

    def _resolve_model_dir(self, preferred):
        """First existing dir among (config, shared convention, staging)."""
        for d in (preferred, SHARED_MODEL_DIR, STAGING_MODEL_DIR):
            if d and os.path.isdir(d):
                return d
        # nothing exists yet: return the shared convention so the error message
        # points at the intended location.
        return preferred or SHARED_MODEL_DIR

    # -- event plumbing -------------------------------------------------------- #
    def _on_voice_event(self, ev):
        """VoiceStateMachine callback -> shape + publish one event.

        The state machine emits {"type": state|wake|transcript|listen_timeout, ...}.
        We mirror `type` into `kind` (the field the /appcenter event log + MQTT
        summary key off), and attach a top-level `state` + `summary{state,text}`
        so the panel and Home Assistant always see the current state and the last
        transcript regardless of which event arrived.

        This is this app's "loop body": the frame-driven apps tick + emit once
        per frame, this one does it once per voice event.
        """
        # Apply any pending SIGHUP config hot-reload at an event boundary. The
        # frame-driven shape gets this from self.frames(); a self-paced run()
        # calls it itself (spec §2, `self.tick()`).
        self.tick()
        kind = ev.get("type", "event")
        out = dict(ev)
        out["kind"] = kind
        if kind == "state":
            self._state = ev.get("state", self._state)
        elif kind == "transcript":
            self._last_text = ev.get("text", "") or self._last_text
        # Same emit path as every other app: kit publishes through the sinks
        # assembled from the manifest `output` block.
        self.emit([out], float(ev.get("t", 0.0)),
                  extra={"state": self._state,
                         "summary": {"state": self._state,
                                     "text": self._last_text}})

    # -- main loop (self-paced: audio chunks, not frames) ---------------------- #
    def run(self):
        from kit.asr import Asr
        from kit.logic.vad import VadSegmenter
        from kit.logic.wakeword import SherpaKwsWakeWord, AsrKeywordWakeWord
        from kit.logic.voice_sm import VoiceStateMachine

        verbose = self.verbose
        md = self.model_dir
        asr_model = os.path.join(md, "model.int8.onnx")
        asr_tokens = os.path.join(md, "tokens.txt")
        vad_model = os.path.join(md, "silero_vad.onnx")
        kws_dir = os.path.join(md, "kws")

        if verbose:
            print(f"[app:{self.id}] model_dir={md} backend={self.wake_backend} "
                  f"lang={self.language} min_silence={self.min_silence_sec} "
                  f"max_utt={self.max_utterance_sec} preroll_ms={self.preroll_ms}",
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
                # `self.source_url` is the CLI `--url` kit was started with (the
                # same value the pre-migration run(url=...) parameter carried).
                rtsp = self.source_url or self.config.get("rtsp_url") \
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
