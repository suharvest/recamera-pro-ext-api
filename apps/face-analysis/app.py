#!/usr/bin/env python3
"""
face-analysis -- reCamera Pro three-stage face cascade (port of the first-gen
SSCMA face-analysis / audience-analytics solution).

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1/§3): `run()`
owns the loop and the whole three-stage cascade reads top to bottom as ordinary
Python -- no cascade framework, no declarative stage list:

  frame -> self.pre()             letterbox to 640 (manifest models[0].input)
        -> self.models.det        YOLOv8n-face rawhead
        -> face_post.postprocess  face boxes in ORIGINAL pixels, score-desc
        -> results[:max_faces]    ★business★ top-K faces
        -> for each face:         <-- stages 2+3 are a PLAIN `for`, spec §3
             crop_square_roi      padded SQUARE ROI out of the FULL-RES frame
             self.models.fairface_fp16        (1,18) -> race/gender/age heads
             self.models.emotion_enet_b0_fp16 (1,8)  -> AffectNet emotion,
                                  ★business★ only every `emotion_interval`
                                  frames; the last verdict is cached per face
                                  index and reused in between
        -> time-windowed demographic histogram (★business★, cross-frame)
        -> self.emit()            one `face` event per face + the periodic
                                  `demographics` aggregate + results[]

`model_frame` stays "cpu" ON PURPOSE. Stages 2/3 crop SOURCE-RESOLUTION pixels
out of `frame.data`, so "hw-direct" -- which letterboxes into `frame.data`
itself -- would silently feed the classifiers a 640x640 model image instead of
the camera frame. A 2026-08-14 device A/B also measured "hw" at +0.8% (noise)
because it still pays the full-res convert plus an extra RGA resize. See
docs/guide/hw-preprocess.md before touching this.

All three models are declared in the manifest `models[]` and preloaded by the
kit, so the hand-written "scan the manifest for role==stage2_fairface" loop is
gone. Both classifiers claim the `classify` task, so they are reached by their
manifest ids (an alias claimed twice is dropped rather than resolved
arbitrarily -- see kit.app.ModelRegistry).

Every knob (confidence / iou / max_faces / crop_pad / emotion_interval /
aggregate_window_sec / privacy_blur) is auto-bound from the manifest
config_schema and re-bound on SIGHUP for the apply:"live" ones, so there is no
setup() param-copying and no on_config_reload.

ImageNet normalization is baked into both classifier rknns, so we feed the raw
uint8 224x224 RGB ROI straight to the engine (no /255, no mean/std here).

Run on device (inference requires root):
    python3 -m kit.run /userdata/local/apps/face-analysis \
        --model models/yolov8n_face_rawhead_fp16.rknn --sink ws --port 8124
"""

from kit.app import App, run_app
from kit import events as E
from kit.pipeline import crop_square_roi
from kit.runtime.postprocess import face_detect as face_post
from kit.runtime.postprocess import classify as clf

# Classifier input sides (manifest models[1].input / models[2].input).
FF_INPUT = 224
EMO_INPUT = 224

# Manifest model ids. Both classifiers declare task "classify", so the `.cls`
# alias is ambiguous and deliberately dropped by the registry.
FF_ID = "fairface_fp16"
EMO_ID = "emotion_enet_b0_fp16"


class FaceAnalysisApp(App):
    id = "face-analysis"
    name = "Face Analysis"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Stages 2/3 crop original-resolution pixels out of frame.data -- see the
    # module docstring for why this must not become "hw"/"hw-direct".
    model_frame = "cpu"

    # Fallbacks for the auto-bound config_schema keys (used when a key is
    # missing from the effective config; the manifest supplies each default).
    confidence = 0.4
    max_faces = 5
    crop_pad = 0.15
    emotion_interval = 1
    aggregate_window_sec = 30.0
    privacy_blur = True

    def setup(self, config):
        """Build the cross-frame aggregation state from the already-bound params.

        Called by `App.start()` AFTER the config_schema auto-bind, so every
        `self.<knob>` below is already populated. All three RKNNs are preloaded
        by the kit from the manifest `models[]`.
        """
        super().setup(config)

        # --- time-windowed demographic aggregation (cross-frame state) ---- #
        self._win_start = None
        self._win_faces = 0
        self._hist = {"gender": {}, "age": {}, "race": {}, "emotion": {}}
        self._frame_idx = 0
        self._emotion_cache = {}   # face-index -> emotion dict (reused between runs)

        print(f"[face-analysis] setup conf={self.confidence} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"fairface={FF_ID}({FF_INPUT}) emotion={EMO_ID}({EMO_INPUT}) "
              f"emotion_interval={self.emotion_interval} "
              f"agg_window={self.aggregate_window_sec}s "
              f"privacy_blur={self.privacy_blur}", flush=True)

    def on_params_changed(self, changed):
        """★S1 live hot-reload★ -- after SIGHUP re-bound the apply:"live" keys.

        face-analysis owns NO derived object that needs rebuilding: every live
        knob (confidence / iou / max_faces / crop_pad / emotion_interval /
        privacy_blur) is a plain scalar the auto-bind has already replaced on
        `self`, and each is read fresh inside the loop (`max_faces` and
        `emotion_interval` are typed "number" in the schema, so they arrive as
        floats and are clamped/int-ed at the use site, exactly as the old
        on_config_reload did). Nothing here touches `self._win_start` /
        `self._hist` / `self._emotion_cache`, so the aggregation window and the
        cached emotions survive a config change untouched.
        `aggregate_window_sec` is apply:"restart" and never reaches here.
        """
        print(f"[face-analysis] hot-reload changed={sorted(changed)} "
              f"conf={self.confidence} iou={self.iou} "
              f"max_faces={self.max_faces} crop_pad={self.crop_pad} "
              f"emotion_interval={self.emotion_interval} "
              f"privacy_blur={self.privacy_blur}", flush=True)

    # -- helpers (business: the demographic window) ------------------------ #
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
        if (t - self._win_start) < self.aggregate_window_sec:
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

    def run(self):
        for frame in self.frames():
            # -- 1. pre / infer / stage-1 post --------------------------- #
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            results = face_post.postprocess(outs, x.info,
                                            conf_thres=self.confidence,
                                            iou_thres=self.iou)

            # ★business★ cross-frame frame counter drives the emotion cadence.
            self._frame_idx += 1
            t = frame.pts
            interval = max(1, int(self.emotion_interval))
            run_emotion = (self._frame_idx % interval) == 0

            # -- 2. stages 2+3: one padded square ROI per face ----------- #
            # A plain Python loop, not a declared pipeline stage. `frame.data`
            # is the ORIGINAL camera frame (model_frame="cpu"), which is what
            # crop_square_roi must cut from.
            faces = results[: int(self.max_faces)]
            for i, r in enumerate(faces):
                r["kind"] = "face"
                r["blur"] = self.privacy_blur

                roi, _roi_map = crop_square_roi(frame.data, r["box"],
                                                FF_INPUT, self.crop_pad)

                # stage 2: FairFace age / gender / race
                ff = clf.fairface_decode(self.models[FF_ID].infer(roi))
                r["gender"] = ff["gender"]["label"]
                r["gender_conf"] = ff["gender"]["confidence"]
                r["age"] = ff["age"]["label"]
                r["age_conf"] = ff["age"]["confidence"]
                r["race"] = ff["race"]["label"]
                r["race_conf"] = ff["race"]["confidence"]

                # stage 3: ★business★ emotion runs every `interval` frames; in
                # between, the previous verdict for this face SLOT is reused.
                if run_emotion:
                    if EMO_INPUT != FF_INPUT:
                        roi_e, _ = crop_square_roi(frame.data, r["box"],
                                                   EMO_INPUT, self.crop_pad)
                    else:
                        roi_e = roi
                    em = clf.emotion_decode(self.models[EMO_ID].infer(roi_e))
                    self._emotion_cache[i] = em
                em = self._emotion_cache.get(i)
                if em is not None:
                    r["emotion"] = em["label"]
                    r["emotion_conf"] = em["confidence"]

                # ★business★ feed the running demographic histogram
                self._win_faces += 1
                self._bump("gender", r.get("gender"))
                self._bump("age", r.get("age"))
                self._bump("race", r.get("race"))
                if em is not None:
                    self._bump("emotion", em["label"])

            # -- 3. events: one attribute event per face ----------------- #
            events = [E.face_attributes(r, blur=self.privacy_blur)
                      for r in faces]

            # ★business★ time-windowed demographic aggregation
            agg = self._roll_window(t)
            if agg is not None:
                events.append(agg)
                print(f"[face-analysis] demographics window={agg['window_sec']}s "
                      f"faces={agg['faces']} gender={agg['gender']} "
                      f"age={agg['age']} emotion={agg['emotion']}", flush=True)

            self.emit(events, frame.pts, results=results)


if __name__ == "__main__":
    run_app(FaceAnalysisApp())
