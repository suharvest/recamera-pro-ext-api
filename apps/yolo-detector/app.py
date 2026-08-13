#!/usr/bin/env python3
"""
yolo-detector -- the first reCamera Pro self-hosted app (kit-design §6).

Deliberately thin: all of frame grab / preprocess / RKNN infer / detect
post-process / result publishing lives in the Kit base loop (`kit.app.App`).
This app only:
  * declares which model + post-processor it uses (mirrors manifest.json), and
  * overrides on_results() to shape raw detections into app-level "detection"
    events (no tracking yet -- that is a later, still-generic, logic-lib add).

Run on device (inference requires root):

    KIT=/userdata/local/apps/kit          # kit/ dir on sys.path
    PYTHONPATH=$KIT python3 app.py \
        --model models/yolo8n_rawhead_int8.rknn --conf 0.35

Publishes per-frame JSON to a local WebSocket (default 0.0.0.0:8124) for the
/appcenter overlay to subscribe to; use --sink stdout for plain debug output.
"""
import os
import sys

# Make the shared Kit package importable whether launched by appmgr or by hand.
# We add the directory that CONTAINS `kit/` to sys.path, then `import kit.*`.
# (Adding kit/ itself would let this file's own name `app` shadow `kit.app`.)
_here = os.path.dirname(os.path.abspath(__file__))
_kit_parent_env = os.environ.get("KIT_PARENT")
_kit_dir_env = os.environ.get("KIT_DIR")
for _cand in (
    _kit_parent_env,
    os.path.dirname(_kit_dir_env) if _kit_dir_env else None,
    os.path.join(_here, ".."),                       # device: /userdata/local/apps
    os.path.join(_here, "..", ".."),                 # repo: recamera_pro/
    "/userdata/local/apps",                          # device fallback
):
    if _cand and os.path.isdir(os.path.join(_cand, "kit")):
        _cand = os.path.abspath(_cand)
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from kit.app import App, run_app  # noqa: E402


class YoloDetectorApp(App):
    id = "yolo-detector"
    name = "YOLO Detector"
    postproc = "detect"
    # Only boxes are consumed -- never frame.data pixels -- so the frame source
    # can letterbox on RGA (see App.direct_model_frame).
    direct_model_frame = True

    # on_config_reload is intentionally NOT overridden: yolo-detector's only
    # apply:"live" knobs are `conf`/`iou`, which the base App.on_config_reload
    # already value-replaces (never rebuilding the model).

    def on_results(self, results, frame):
        """Format each detection into a flat, overlay-friendly event.

        Kept intentionally minimal for v1 (no tracking / counting). Downstream
        panels get stable fields: class, confidence, and a pixel box in the
        ORIGINAL frame coordinate space (post-process already un-letterboxed).
        """
        return [
            {
                "kind": "detection",
                "label": d["cls_name"],
                "cls": d["cls"],
                "score": d["score"],
                "box": d["box"],   # [x1,y1,x2,y2] in original-frame pixels
            }
            for d in results
        ]


if __name__ == "__main__":
    run_app(YoloDetectorApp())
