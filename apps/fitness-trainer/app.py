#!/usr/bin/env python3
"""
fitness-trainer -- reCamera Pro pose app (port of the first-gen SSCMA solution).

Pipeline (all shared parts live in kit.app.App):
  live frame -> letterbox 640 -> YOLO11n-pose RKNN -> pose post-process
  -> on_results(): pick primary subject, feed its keypoints to the selected
     exercise's joint-angle hysteresis state machine (kit.logic.rep_counter)
     -> emit keypoints + a `workout` event with rep/set counts, stage, angle
     and any form warning.

Only the pose post-processor selection and the on_results() business logic are
app-specific; everything else is the generic Kit loop.

The workout + detection parameters come from THIS app's manifest.json
config_schema (mode / target_reps / target_sets / idle_reset_seconds +
confidence / keypoint_confidence). appmgr's minimal launcher forwards only
--model / --sink / --port, so the app reads its own manifest for the rest --
mirrors the fall-detection app and matches the first-gen schema exactly.

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
from kit.logic.rep_counter import create_exercise, exercise_ids   # noqa: E402


class FitnessTrainerApp(App):
    id = "fitness-trainer"
    name = "Fitness Trainer"
    postproc = "pose"
    # Only skeleton coordinates are consumed -- never frame.data pixels -- so the
    # frame source can letterbox on RGA (see App.direct_model_frame).
    direct_model_frame = True

    def setup(self, config):
        super().setup(config)
        # `config` is the effective config from kit.config (manifest defaults
        # overlaid by <app_dir>/config.json) -- single unified entry.
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # Person / keypoint confidence for the pose post-process.
        self.conf = float(params.get("confidence", 0.4))
        self.kpt_thres = float(params.get("keypoint_confidence", 0.5))

        mode = str(params.get("mode", "squat"))
        self.target_reps = int(params.get("target_reps", 12))
        self.target_sets = int(params.get("target_sets", 3))
        self.idle_reset_seconds = float(params.get("idle_reset_seconds", 60))

        self.exercise = create_exercise(mode, self.kpt_thres)
        if self.exercise is None:
            print(f"[fitness] WARN unknown mode {mode!r}, falling back to squat "
                  f"(known: {exercise_ids()})", file=sys.stderr, flush=True)
            mode = "squat"
            self.exercise = create_exercise(mode, self.kpt_thres)
        self.mode = mode
        self.exercise.set_targets(self.target_reps, self.target_sets)

        self._last_person_pts = None   # last frame pts a subject was seen at
        self._last_workout_complete = False

        print(f"[fitness] setup mode={self.mode} target={self.target_reps}x"
              f"{self.target_sets} conf={self.conf} kpt_thres={self.kpt_thres} "
              f"idle_reset={self.idle_reset_seconds}s", flush=True)

    def on_config_reload(self, config):
        """★S1 live hot-reload★ (SIGHUP -> re-read config.json).

        fitness-trainer live knobs: confidence / keypoint_confidence (thresholds)
        and mode / target_reps / target_sets / idle_reset_seconds. Reapply by
        VALUE-REPLACE, preserving the rep/set accumulator:
          * target_reps/target_sets -> set_targets() (does NOT reset counts).
          * keypoint_confidence     -> mutate self.exercise.kpt_thres in place.
          * mode                    -> only build a NEW exercise when it actually
            changes (a different exercise is a different state machine; reps for
            squat do not carry to push-up). An unchanged mode keeps the running
            accumulator untouched.
        """
        params = self._reload_params(config)
        self.config = config or {}
        self.conf = self._reload_float(params, "confidence", self.conf)
        self.kpt_thres = self._reload_float(params, "keypoint_confidence", self.kpt_thres)
        self.target_reps = self._reload_int(params, "target_reps", self.target_reps)
        self.target_sets = self._reload_int(params, "target_sets", self.target_sets)
        self.idle_reset_seconds = self._reload_float(
            params, "idle_reset_seconds", self.idle_reset_seconds)

        new_mode = str(params.get("mode", self.mode))
        if new_mode != self.mode:
            ex = create_exercise(new_mode, self.kpt_thres)
            if ex is None:
                print(f"[fitness] hot-reload unknown mode {new_mode!r}, keeping "
                      f"{self.mode!r}", file=sys.stderr, flush=True)
            else:
                self.exercise = ex
                self.mode = new_mode
        else:
            # same exercise: apply the live keypoint threshold in place.
            self.exercise.kpt_thres = self.kpt_thres
        # targets are always safe to (re)apply -- set_targets keeps current reps.
        self.exercise.set_targets(self.target_reps, self.target_sets)
        print(f"[fitness] hot-reload mode={self.mode} "
              f"target={self.target_reps}x{self.target_sets} conf={self.conf} "
              f"kpt_thres={self.kpt_thres} idle_reset={self.idle_reset_seconds}s",
              flush=True)

    def run_postproc(self, outs, info):
        return pose_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou,
                                     kpt_thres=self.kpt_thres)

    def on_results(self, results, frame):
        # Primary subject = highest-scoring person (post-process already sorted).
        primary = results[0] if results else None
        now = frame.pts

        # Idle reset: after idle_reset_seconds with nobody in view, zero the
        # workout so the next athlete starts fresh (0 = never, first-gen behaviour).
        if primary is not None:
            self._last_person_pts = now
        elif (self.idle_reset_seconds > 0 and self._last_person_pts is not None
              and now - self._last_person_pts >= self.idle_reset_seconds):
            self.exercise.reset()
            self.exercise.set_targets(self.target_reps, self.target_sets)
            self._last_person_pts = None
            self._last_workout_complete = False
            print(f"[fitness] idle reset after "
                  f"{self.idle_reset_seconds:.0f}s with no subject", flush=True)

        st = self.exercise.update(primary, now)

        event = {
            "kind": "workout",
            "mode": self.mode,
            "target_reps": self.target_reps,
            "target_sets": self.target_sets,
            "person_score": round(float(primary["score"]), 3) if primary else 0.0,
        }
        event.update(st.as_dict())
        events = [event]

        if st.rep_completed:
            print(f"[fitness] rep -> set {st.set} reps {st.reps}/"
                  f"{self.target_reps} (stage={st.stage} angle={st.angle:.0f})"
                  + (f" [{st.form_warning}]" if st.form_warning else ""),
                  flush=True)
        if st.workout_complete and not self._last_workout_complete:
            print(f"[fitness] *** WORKOUT COMPLETE at pts={now:.2f} ***",
                  flush=True)
        self._last_workout_complete = st.workout_complete

        for r in results:
            r.setdefault("kind", "person")
        return events


if __name__ == "__main__":
    run_app(FitnessTrainerApp())
