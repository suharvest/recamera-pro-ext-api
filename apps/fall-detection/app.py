#!/usr/bin/env python3
"""
fall-detection -- reCamera Pro pose app (port of the first-gen SSCMA solution).

Pipeline (all shared parts live in kit.app.App):
  live frame -> letterbox 640 -> YOLO11n-pose RKNN -> pose post-process
  -> on_results(): pick primary subject, build a geometric Observation
     (hip_y / torso angle / box aspect) and advance the ported temporal
     FallDetector state machine -> emit keypoints + pose_state + fall events.

Only the pose post-processor selection and the on_results() business logic are
app-specific; everything else is the generic Kit loop.

The 12 tuning parameters come from THIS app's manifest.json config_schema
(grouped detection/timing). appmgr's minimal launcher only forwards --model /
--sink / --port, so the app reads its own manifest for the fall thresholds --
this keeps appmgr generic and matches the first-gen 12-param schema exactly.

Run on device (inference requires root):
    KIT=/userdata/local/kit
    PYTHONPATH=$KIT python3 app.py \
        --model models/yolo11n_pose_rawhead_int8.rknn --sink ws --port 8124
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

from kit.app import App, run_app                                  # noqa: E402
from kit.runtime.postprocess import pose as pose_post             # noqa: E402
from kit.logic.geometry import make_observation, N_KPT            # noqa: E402
from kit.logic.temporal import FallDetector, FallConfig, FALLEN, RECOVERING  # noqa: E402


class FallDetectionApp(App):
    id = "fall-detection"
    name = "Fall Detection"
    postproc = "pose"

    def setup(self, config):
        super().setup(config)
        # `config` is the effective config assembled by kit.config in run_app:
        # manifest config_schema defaults overlaid by <app_dir>/config.json.
        # The 12 fall-tuning params are read straight from it (single entry).
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # Person / keypoint confidence for the pose post-process.
        self.conf = float(params.get("confidence", 0.4))
        self.kpt_thres = float(params.get("keypoint_confidence", 0.5))

        cfg = FallConfig(
            hip_drop_speed_threshold=float(params.get("hip_drop_speed_threshold", 0.25)),
            hip_drop_distance_threshold=float(params.get("hip_drop_distance_threshold", 0.02)),
            motion_window_sec=float(params.get("motion_window_sec", 0.75)),
            torso_angle_threshold_deg=float(params.get("torso_angle_threshold_deg", 55.0)),
            bbox_aspect_ratio_threshold=float(params.get("bbox_aspect_ratio_threshold", 1.25)),
            min_suspected_features=int(params.get("min_suspected_features", 2)),
            confirmation_sec=float(params.get("confirmation_sec", 0.80)),
            suspected_timeout_sec=float(params.get("suspected_timeout_sec", 1.50)),
            occlusion_grace_sec=float(params.get("occlusion_grace_sec", 0.75)),
            recovery_torso_angle_deg=float(params.get("recovery_torso_angle_deg", 35.0)),
            recovery_aspect_ratio=float(params.get("recovery_aspect_ratio", 1.10)),
            recovery_window_sec=float(params.get("recovery_window_sec", 2.00)),
            cooldown_sec=float(params.get("cooldown_sec", 3.00)),
        )
        self.detector = FallDetector(cfg)
        print(f"[fall] setup conf={self.conf} kpt_thres={self.kpt_thres} "
              f"torso>={cfg.torso_angle_threshold_deg} aspect>="
              f"{cfg.bbox_aspect_ratio_threshold} confirm={cfg.confirmation_sec}s",
              flush=True)

    def run_postproc(self, outs, info):
        return pose_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou,
                                     kpt_thres=self.kpt_thres)

    def on_results(self, results, frame):
        # Primary subject = highest-scoring person (post-process already sorted).
        primary = results[0] if results else None
        obs = make_observation(primary, frame.pts, frame.h, self.kpt_thres)
        out = self.detector.update(obs)

        events = []
        # Always surface the current pose state so an overlay can render it.
        events.append({
            "kind": "pose_state",
            "state": out.state,
            "fall_detected": out.fall_detected,
            "event_id": out.event_id,
            "person_score": round(obs.person_score, 3),
            "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 1),
            "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 3),
            "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 3),
            "evidence_features": int(out.diagnostics.get("evidence_features", 0)),
        })
        # Edge event: a fall was just confirmed this frame.
        if out.fall_event:
            events.append({
                "kind": "fall",
                "event_id": out.event_id,
                "state": out.state,
                "box": primary["box"] if primary else None,
            })
            print(f"[fall] *** FALL event #{out.event_id} at pts={frame.pts:.2f} ***",
                  flush=True)

        # Attach the primary subject's keypoints to each result for overlay use
        # (already in original-frame pixels from the post-process).
        for r in results:
            r.setdefault("kind", "person")
        return events


if __name__ == "__main__":
    run_app(FallDetectionApp())
