#!/usr/bin/env python3
"""
face-analysis -- reCamera Pro three-stage face cascade (port of the first-gen
SSCMA face-analysis / audience-analytics solution).

Cascade pipeline (reusing the kit.pipeline scaffold that facemesh-reader
introduced):

  live frame -> letterbox 640 -> YOLOv8n-face RKNN -> face_detect post-process
     (stage 1, handled by the generic Kit base loop -> face boxes in orig px)
  -> on_results(): for each face box (top-K by score),
       crop a padded SQUARE 224 ROI from the ORIGINAL frame
       (kit.pipeline.crop_square_roi, the core of CascadePipeline), then run
       BOTH classifiers on that one ROI:
         stage 2  FairFace RKNN (1,18) -> classify.fairface_decode
                  -> race / gender / age-band, each its own softmax+argmax head
         stage 3  emotion enet_b0 RKNN (1,8) -> classify.emotion_decode
                  -> 8-class AffectNet emotion softmax
  -> per-face attribute event + a simple time-windowed demographic aggregation
     (gender / age / race / emotion histograms), emitted once per window.

ImageNet normalization is baked into both classifier rknns, so we feed the raw
uint8 224x224 RGB ROI straight to the engine (no /255, no mean/std here).

appmgr's supervisor launches `app.py --model models[0].file --sink ws --port`
(stage-1 model only), so this app loads its OWN stage-2/3 classifier models in
setup() by reading models[1]/models[2] from its manifest -- the same pattern
facemesh-reader / fall-detection use. appmgr stays generic.

Run on device (inference requires root):
    KIT=/userdata/local/kit
    PYTHONPATH=$KIT python3 app.py \
        --model models/yolov8n_face_rawhead_fp16.rknn --sink ws --port 8124
"""
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_kit_parent_env = os.environ.get("KIT_PARENT")
_kit_dir_env = os.environ.get("KIT_DIR")
for _cand in (
    _kit_parent_env,
    os.path.dirname(_kit_dir_env) if _kit_dir_env else None,
    "/userdata/local",                               # device: kit at /userdata/local/kit
    os.path.join(_here, ".."),                       # device: /userdata/local/apps
    os.path.join(_here, "..", ".."),                 # repo: recamera_pro/
    "/userdata/local/apps",
):
    if _cand and os.path.isdir(os.path.join(_cand, "kit")):
        _cand = os.path.abspath(_cand)
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from kit.app import App, run_app                                       # noqa: E402
from kit.pipeline import crop_square_roi                               # noqa: E402
from kit.runtime.engine import RknnModel                              # noqa: E402
from kit.runtime.postprocess import face_detect as face_post          # noqa: E402
from kit.runtime.postprocess import classify as clf                   # noqa: E402


class FaceAnalysisApp(App):
    id = "face-analysis"
    name = "Face Analysis"
    postproc = "face_detect"
    # Stays on "cpu": these crop source-resolution pixels, so "hw-direct" is
    # unsafe, and a 2026-08-14 device A/B measured "hw" at +0.8% (noise) --
    # it still pays the full-res convert plus an extra RGA resize.
    # See docs/guide/hw-preprocess.md before switching.

    def setup(self, config):
        super().setup(config)
        manifest = {}
        try:
            with open(os.path.join(_here, "manifest.json")) as f:
                manifest = json.load(f)
        except Exception as e:                                        # pragma: no cover
            print(f"[face-analysis] WARN could not read manifest: {e}",
                  file=sys.stderr, flush=True)

        # Config from the unified effective config (kit.config); manifest is
        # read only for the stage-2/3 classifier models below.
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # --- stage-1 face detection thresholds ---
        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))
        self.max_faces = int(params.get("max_faces", 5))
        self.crop_pad = float(params.get("crop_pad", 0.15))

        # --- attribute analysis knobs ---
        self.emotion_interval = max(1, int(params.get("emotion_interval", 1)))
        self.agg_window = float(params.get("aggregate_window_sec", 30.0))
        self.privacy_blur = bool(params.get("privacy_blur", True))

        # --- stage-2 / stage-3 classifier models (models[1], models[2]) ------ #
        # appmgr only passes models[0]; we load the extra classifiers ourselves.
        ff_file, ff_input = "models/fairface_fp16.rknn", 224
        emo_file, emo_input = "models/emotion_enet_b0_fp16.rknn", 224
        for m in manifest.get("models", []):
            role, task = m.get("role"), m.get("task")
            inp = m.get("input")
            side = int(inp[1]) if isinstance(inp, list) and len(inp) == 4 else None
            if role == "stage2_fairface" or m.get("id", "").startswith("fairface"):
                ff_file = m.get("file", ff_file)
                if side:
                    ff_input = side
            elif role == "stage3_emotion" or m.get("id", "").startswith("emotion"):
                emo_file = m.get("file", emo_file)
                if side:
                    emo_input = side

        def _abs(p):
            return p if os.path.isabs(p) else os.path.join(_here, p)

        self.ff_input = ff_input
        self.emo_input = emo_input
        self.ff_model = RknnModel(_abs(ff_file))
        self.emo_model = RknnModel(_abs(emo_file))

        # --- simple time-windowed demographic aggregation state ---
        self._win_start = None
        self._win_faces = 0
        self._hist = {"gender": {}, "age": {}, "race": {}, "emotion": {}}
        self._frame_idx = 0
        self._emotion_cache = {}   # face-index -> emotion dict (reused between runs)

        print(f"[face-analysis] setup conf={self.conf} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"fairface={os.path.basename(ff_file)}({ff_input}) "
              f"emotion={os.path.basename(emo_file)}({emo_input}) "
              f"emotion_interval={self.emotion_interval} "
              f"agg_window={self.agg_window}s privacy_blur={self.privacy_blur}",
              flush=True)

    def on_config_reload(self, config):
        """★S1 live hot-reload★ (SIGHUP -> re-read config.json).

        face-analysis keeps its live knobs under app-specific keys the base
        App.on_config_reload does not know (`confidence` not `conf`, plus
        max_faces / crop_pad / emotion_interval / privacy_blur). Reapply them
        here by VALUE-REPLACE only -- never reloading the stage-1/2/3 models or
        resetting the demographic aggregation window. `aggregate_window_sec` is
        apply:"restart" and is intentionally NOT touched here.
        """
        params = self._reload_params(config)
        self.config = config or {}
        self.conf = self._reload_float(params, "confidence", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)
        self.max_faces = self._reload_int(params, "max_faces", self.max_faces)
        self.crop_pad = self._reload_float(params, "crop_pad", self.crop_pad)
        self.emotion_interval = max(
            1, self._reload_int(params, "emotion_interval", self.emotion_interval))
        if "privacy_blur" in params:
            self.privacy_blur = bool(params["privacy_blur"])
        print(f"[face-analysis] hot-reload conf={self.conf} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"emotion_interval={self.emotion_interval} "
              f"privacy_blur={self.privacy_blur}", flush=True)

    def run_postproc(self, outs, info):
        return face_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou)

    # -- helpers ---------------------------------------------------------- #
    def _bump(self, head: str, label) -> None:
        if label is None:
            return
        d = self._hist[head]
        d[label] = d.get(label, 0) + 1

    def _roll_window(self, t: float):
        """Emit a demographics aggregate event when the window elapses; reset."""
        if self._win_start is None:
            self._win_start = t
            return None
        if (t - self._win_start) < self.agg_window:
            return None
        event = {
            "kind": "demographics",
            "window_sec": round(float(t - self._win_start), 1),
            "faces": int(self._win_faces),
            "gender": dict(self._hist["gender"]),
            "age": dict(self._hist["age"]),
            "race": dict(self._hist["race"]),
            "emotion": dict(self._hist["emotion"]),
        }
        # reset window
        self._win_start = t
        self._win_faces = 0
        for k in self._hist:
            self._hist[k] = {}
        return event

    # -- business logic --------------------------------------------------- #
    def on_results(self, results, frame):
        self._frame_idx += 1
        t = frame.pts
        run_emotion = (self._frame_idx % self.emotion_interval) == 0

        faces = results[: self.max_faces]
        for i, r in enumerate(faces):
            r["kind"] = "face"
            r["blur"] = self.privacy_blur

            roi, _roi_map = crop_square_roi(frame.data, r["box"],
                                            self.ff_input, self.crop_pad)

            # stage 2: FairFace age / gender / race
            ff = clf.fairface_decode(self.ff_model.infer(roi))
            r["gender"] = ff["gender"]["label"]
            r["gender_conf"] = ff["gender"]["confidence"]
            r["age"] = ff["age"]["label"]
            r["age_conf"] = ff["age"]["confidence"]
            r["race"] = ff["race"]["label"]
            r["race_conf"] = ff["race"]["confidence"]

            # stage 3: emotion (optionally every N frames; cache between runs)
            if run_emotion:
                if self.emo_input != self.ff_input:
                    roi_e, _ = crop_square_roi(frame.data, r["box"],
                                               self.emo_input, self.crop_pad)
                else:
                    roi_e = roi
                em = clf.emotion_decode(self.emo_model.infer(roi_e))
                self._emotion_cache[i] = em
            em = self._emotion_cache.get(i)
            if em is not None:
                r["emotion"] = em["label"]
                r["emotion_conf"] = em["confidence"]

            # feed the running demographic histogram
            self._win_faces += 1
            self._bump("gender", r.get("gender"))
            self._bump("age", r.get("age"))
            self._bump("race", r.get("race"))
            if em is not None:
                self._bump("emotion", em["label"])

        events = []
        # one attribute event per detected face (the primary payload)
        for r in faces:
            events.append({
                "kind": "face",
                "box": r["box"],
                "score": r.get("score"),
                "gender": r.get("gender"),
                "gender_conf": r.get("gender_conf"),
                "age": r.get("age"),
                "age_conf": r.get("age_conf"),
                "race": r.get("race"),
                "race_conf": r.get("race_conf"),
                "emotion": r.get("emotion"),
                "emotion_conf": r.get("emotion_conf"),
                "blur": bool(self.privacy_blur),
            })

        # simple time-windowed demographic aggregation
        agg = self._roll_window(t)
        if agg is not None:
            events.append(agg)
            print(f"[face-analysis] demographics window={agg['window_sec']}s "
                  f"faces={agg['faces']} gender={agg['gender']} "
                  f"age={agg['age']} emotion={agg['emotion']}", flush=True)

        return events


if __name__ == "__main__":
    run_app(FaceAnalysisApp())
