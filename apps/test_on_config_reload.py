"""
Unit tests for per-app on_config_reload() live hot-reload overrides (S1).

These prove the value-replace contract: a LIVE config change re-assigns the
app's runtime attributes WITHOUT rebuilding the model / pipeline / accumulator.

Only apps whose module imports cleanly off-device are exercised here (no rknnlite
required): fitness-trainer (rich: mode / targets / thresholds, with an exercise
state machine) and yolo-detector (delegates conf/iou to the base App). The other
apps' overrides follow the identical value-replace pattern and are covered by
py_compile + code review.

Run:  python3 apps/test_on_config_reload.py     (from repo root, or via pytest)
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load(app_dir):
    path = os.path.join(_ROOT, "apps", app_dir, "app.py")
    spec = importlib.util.spec_from_file_location(f"{app_dir}_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fitness_reload_value_replaces_without_rebuilding_accumulator():
    mod = _load("fitness-trainer")
    from kit.logic.rep_counter import create_exercise

    app = mod.FitnessTrainerApp()
    # Hand-wire the attributes setup() would populate (skip model load).
    app.conf = 0.4
    app.kpt_thres = 0.5
    app.mode = "squat"
    app.target_reps = 12
    app.target_sets = 3
    app.idle_reset_seconds = 60.0
    app.exercise = create_exercise("squat", 0.5)
    app.exercise.set_targets(12, 3)
    exercise_before = app.exercise
    # simulate an accumulated rep so we can prove state is preserved
    app.exercise.state.reps = 7

    # 1) threshold + target change, SAME mode -> attrs replaced, exercise object
    #    is the SAME instance (accumulator NOT rebuilt), targets updated.
    app.on_config_reload({"confidence": 0.6, "target_reps": 20,
                          "keypoint_confidence": 0.7})
    assert app.conf == 0.6, app.conf
    assert app.kpt_thres == 0.7, app.kpt_thres
    assert app.target_reps == 20, app.target_reps
    assert app.exercise is exercise_before, "exercise must NOT be rebuilt"
    assert app.exercise.kpt_thres == 0.7, app.exercise.kpt_thres   # in-place
    assert app.exercise.target_reps == 20, app.exercise.target_reps
    assert app.exercise.state.reps == 7, "rep accumulator must survive"

    # 2) mode change -> a NEW exercise state machine (semantically required;
    #    squat reps do not carry to push-up).
    app.on_config_reload({"mode": "push_up"})
    assert app.mode == "push_up", app.mode
    assert app.exercise is not exercise_before, "mode change builds new exercise"
    assert app.exercise.id == "push_up", app.exercise.id
    assert app.exercise.target_reps == 20, "targets carried onto new exercise"

    # 3) unknown mode -> keep the current exercise, no crash.
    keep = app.exercise
    app.on_config_reload({"mode": "no_such_mode"})
    assert app.exercise is keep and app.mode == "push_up"
    print("PASS test_fitness_reload_value_replaces_without_rebuilding_accumulator")


def test_yolo_reload_delegates_conf_iou_to_base():
    mod = _load("yolo-detector")
    app = mod.YoloDetectorApp()
    app.conf = 0.35
    app.iou = 0.45
    app.config = {}
    app.on_config_reload({"conf": 0.6, "iou": 0.55})
    assert app.conf == 0.6, app.conf
    assert app.iou == 0.55, app.iou
    # a malformed value is ignored (base guards), previous value kept
    app.on_config_reload({"conf": "bad"})
    assert app.conf == 0.6, app.conf
    print("PASS test_yolo_reload_delegates_conf_iou_to_base")


if __name__ == "__main__":
    test_fitness_reload_value_replaces_without_rebuilding_accumulator()
    test_yolo_reload_delegates_conf_iou_to_base()
    print("ALL APP on_config_reload TESTS PASSED")
