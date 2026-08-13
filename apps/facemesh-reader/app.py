#!/usr/bin/env python3
"""
facemesh-reader -- reCamera Pro two-stage face cascade app (port of the
first-gen SSCMA facemesh-reader / drowsiness solution).

Cascade pipeline (the reusable skeleton lives in kit.pipeline + kit.runtime):

  live frame -> letterbox 640 -> YOLOv8n-face RKNN -> face_detect post-process
     (stage 1, handled by the generic Kit base loop -> boxes in original px)
  -> on_results(): for the primary face box,
       crop padded SQUARE ROI from the ORIGINAL frame -> resize 192 ->
       face_landmark RKNN -> landmark post-process (468x3 mapped back to px)
       (stage 2, kit.pipeline.CascadePipeline)
  -> DrowsinessLogic: EAR / MAR from the 468 points + yawn + PERCLOS temporal
     -> emit metrics + blink / yawn / drowsiness events.

appmgr's supervisor launches `app.py --model models[0].file --sink ws --port`
(stage-1 model only), so this app loads its OWN stage-2 landmark model in
setup() by reading models[1] from its manifest -- exactly the pattern
fall-detection uses to read its extra tuning params. appmgr stays generic.

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
from kit.pipeline import CascadePipeline                               # noqa: E402
from kit.runtime.postprocess import face_detect as face_post          # noqa: E402
from kit.runtime.postprocess import landmark as landmark_post         # noqa: E402
from kit.logic.drowsiness import DrowsinessLogic, DrowsinessConfig     # noqa: E402


class FacemeshReaderApp(App):
    id = "facemesh-reader"
    name = "Facemesh Reader"
    postproc = "face_detect"
    # "hw" (not "hw-direct"): the stage-2 mesh pipeline reads source-resolution
    # pixels from frame.data, so originals must survive; only the letterbox
    # moves to RGA (see App.model_frame).
    model_frame = "hw"

    def setup(self, config):
        super().setup(config)
        manifest = {}
        try:
            with open(os.path.join(_here, "manifest.json")) as f:
                manifest = json.load(f)
        except Exception as e:                                        # pragma: no cover
            print(f"[facemesh] WARN could not read manifest: {e}",
                  file=sys.stderr, flush=True)

        # Config from the unified effective config (kit.config); manifest is
        # read only for the stage-2 landmark model below.
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # --- stage-1 face detection thresholds ---
        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))
        self.crop_pad = float(params.get("crop_pad", 0.25))
        self.presence_threshold = float(params.get("presence_threshold", 0.5))

        # --- stage-2 landmark model (models[1]); appmgr only passes models[0] ---
        lmk_file = "models/face_landmark_fp16.rknn"
        lmk_input = 192
        for m in manifest.get("models", []):
            if m.get("role") == "stage2_landmark" or m.get("task") == "landmark":
                lmk_file = m.get("file", lmk_file)
                inp = m.get("input")
                if isinstance(inp, list) and len(inp) == 4:
                    lmk_input = int(inp[1])
        lmk_path = lmk_file if os.path.isabs(lmk_file) else os.path.join(_here, lmk_file)

        self.pipeline = CascadePipeline(
            model_path=lmk_path,
            input_size=lmk_input,
            decode_fn=landmark_post.decode,
            pad=self.crop_pad,
            max_targets=1,          # primary face drives the drowsiness state
        )

        # --- CPU temporal logic (EAR/MAR + yawn + PERCLOS drowsiness) ---
        cfg = DrowsinessConfig(
            ear_threshold=float(params.get("ear_threshold", 0.21)),
            ear_continuous_sec=float(params.get("ear_continuous_sec", 2.0)),
            perclos_window_sec=float(params.get("perclos_window_sec", 60.0)),
            perclos_critical_pct=float(params.get("perclos_critical_pct", 20.0)),
            alert_cooldown_sec=float(params.get("alert_cooldown_sec", 5.0)),
            yawn_count_threshold=int(params.get("yawn_count_threshold", 3)),
        )
        self.logic = DrowsinessLogic(
            drowsy_cfg=cfg,
            mar_threshold=float(params.get("mar_threshold", 0.65)),
            yawn_consecutive_frames=int(params.get("yawn_consecutive_frames", 5)),
            ear_threshold=float(params.get("ear_threshold", 0.21)),
        )
        # blink edge-detect (eyes_closed rising edge, event-debounced)
        self._prev_closed = False
        self._blink_count = 0
        self._prev_yawn_count = 0

        print(f"[facemesh] setup conf={self.conf} iou={self.iou} "
              f"crop_pad={self.crop_pad} landmark={os.path.basename(lmk_path)} "
              f"input={lmk_input} ear_thr={cfg.ear_threshold} "
              f"mar_thr={self.logic.mar_threshold}", flush=True)

    def on_config_reload(self, config):
        """★S1 live hot-reload★ (SIGHUP -> re-read config.json).

        facemesh-reader stores live knobs under app-specific keys and inside the
        stage-2 CascadePipeline + the CPU DrowsinessLogic. Reapply by VALUE-
        REPLACE only: mutate the existing pipeline/logic objects in place so the
        landmark model and the PERCLOS/yawn accumulator state survive. Structural
        params (`perclos_window_sec`, model input) are apply:"restart" and are
        NOT touched here.
        """
        params = self._reload_params(config)
        self.config = config or {}

        self.conf = self._reload_float(params, "confidence", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)
        self.presence_threshold = self._reload_float(
            params, "presence_threshold", self.presence_threshold)
        # crop_pad drives the stage-2 ROI crop; mutate the pipeline in place
        # (do NOT rebuild -- that would reload the landmark RKNN model).
        self.crop_pad = self._reload_float(params, "crop_pad", self.crop_pad)
        if getattr(self, "pipeline", None) is not None:
            self.pipeline.pad = self.crop_pad

        # EAR / MAR / yawn / drowsiness knobs live inside self.logic (+ its
        # YawnTracker and DrowsinessTracker.cfg). Mutating fields in place keeps
        # every deque / timer intact.
        logic = getattr(self, "logic", None)
        if logic is not None:
            logic.ear_threshold = self._reload_float(
                params, "ear_threshold", logic.ear_threshold)
            logic.mar_threshold = self._reload_float(
                params, "mar_threshold", logic.mar_threshold)
            if getattr(logic, "yawn", None) is not None:
                logic.yawn.mar_threshold = logic.mar_threshold
                logic.yawn.consecutive_frames = self._reload_int(
                    params, "yawn_consecutive_frames", logic.yawn.consecutive_frames)
            cfg = getattr(getattr(logic, "drowsy", None), "cfg", None)
            if cfg is not None:
                cfg.ear_threshold = logic.ear_threshold
                cfg.ear_continuous_sec = self._reload_float(
                    params, "ear_continuous_sec", cfg.ear_continuous_sec)
                cfg.perclos_critical_pct = self._reload_float(
                    params, "perclos_critical_pct", cfg.perclos_critical_pct)
                cfg.alert_cooldown_sec = self._reload_float(
                    params, "alert_cooldown_sec", cfg.alert_cooldown_sec)
                cfg.yawn_count_threshold = self._reload_int(
                    params, "yawn_count_threshold", cfg.yawn_count_threshold)
        print(f"[facemesh] hot-reload conf={self.conf} iou={self.iou} "
              f"crop_pad={self.crop_pad} presence={self.presence_threshold} "
              f"ear_thr={getattr(logic, 'ear_threshold', None)} "
              f"mar_thr={getattr(logic, 'mar_threshold', None)}", flush=True)

    def run_postproc(self, outs, info):
        return face_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou)

    def on_results(self, results, frame):
        # tag every detected face for the overlay
        for r in results:
            r["kind"] = "face"

        t = frame.pts
        primary = results[0] if results else None

        landmarks = None
        presence = 0.0
        if primary is not None:
            stage2 = self.pipeline.process(frame.data, [primary])
            if stage2:
                lm_xyz, presence = stage2[0]["decoded"]
                if presence >= self.presence_threshold:
                    landmarks = lm_xyz     # (468,3) original-frame px

        # Drive the CPU temporal logic (ticks with neutral input when no face).
        metrics, yawn_state, drowsy_state, yawn_event = self.logic.update(landmarks, t)

        events = []

        # Attach per-face metrics + a small landmark summary to the primary result.
        if primary is not None:
            primary["presence"] = round(float(presence), 3)
            primary["landmark_count"] = int(len(landmarks)) if landmarks is not None else 0
            if metrics.valid:
                primary["ear"] = round(metrics.avg_ear, 3)
                primary["mar"] = round(metrics.mar, 3)
                # keypoints as [x,y] pairs for overlay (rounded, sub-sampled off)
                primary["keypoints"] = [
                    [round(float(p[0]), 1), round(float(p[1]), 1)]
                    for p in landmarks
                ] if landmarks is not None else []

        # Always surface the current metrics/state so an overlay can render it.
        events.append({
            "kind": "metrics",
            "face_valid": bool(metrics.valid),
            "avg_ear": round(metrics.avg_ear, 3),
            "left_ear": round(metrics.left_ear, 3),
            "right_ear": round(metrics.right_ear, 3),
            "mar": round(metrics.mar, 3),
            "eyes_closed": bool(metrics.eyes_closed),
            "mouth_open": bool(metrics.mouth_open),
            "state": drowsy_state.state,
            "drowsiness_level": round(drowsy_state.drowsiness_level, 3),
            "perclos_pct": round(drowsy_state.perclos_pct, 1),
            "continuous_closure_sec": round(drowsy_state.continuous_closure_sec, 2),
            "is_yawning": bool(yawn_state.is_yawning_now),
            "yawn_count_5min": int(yawn_state.yawn_count_5min),
            "alert_active": bool(drowsy_state.alert_active),
        })

        # Edge event: blink (eyes-closed rising edge on a valid face).
        if metrics.valid:
            if metrics.eyes_closed and not self._prev_closed:
                self._blink_count += 1
                events.append({"kind": "blink", "blink_count": self._blink_count,
                               "avg_ear": round(metrics.avg_ear, 3)})
            self._prev_closed = metrics.eyes_closed
        else:
            self._prev_closed = False

        # Edge event: yawn onset.
        if yawn_event:
            events.append({"kind": "yawn",
                           "yawn_count_5min": int(yawn_state.yawn_count_5min),
                           "mar": round(metrics.mar, 3)})
            print(f"[facemesh] *** YAWN #{yawn_state.yawn_count_5min} "
                  f"mar={metrics.mar:.2f} at t={t:.2f} ***", flush=True)

        # Edge event: drowsiness alert active (state Drowsy/Danger).
        if drowsy_state.alert_active:
            events.append({
                "kind": "drowsiness",
                "state": drowsy_state.state,
                "drowsiness_level": round(drowsy_state.drowsiness_level, 3),
                "drowsy_by_ear": bool(drowsy_state.drowsy_by_ear),
                "drowsy_by_perclos": bool(drowsy_state.drowsy_by_perclos),
                "drowsy_by_yawn": bool(drowsy_state.drowsy_by_yawn),
                "perclos_pct": round(drowsy_state.perclos_pct, 1),
            })

        return events


if __name__ == "__main__":
    run_app(FacemeshReaderApp())
