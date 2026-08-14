#!/usr/bin/env python3
"""
yolo-detector -- the first reCamera Pro self-hosted app (kit-design §6).

First app migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1):
the whole pipeline is an explicit `run()` loop -- pre / infer / post / emit are
four readable lines of ordinary Python. Frame grab, release, warm-up skipping,
config hot-reload, model loading and output fan-out stay with the kit.

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

from kit.app import App, run_app                                    # noqa: E402
from kit.runtime.postprocess.detect import postprocess               # noqa: E402
from kit import events as E                                          # noqa: E402


class YoloDetectorApp(App):
    id = "yolo-detector"
    name = "YOLO Detector"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Only boxes are consumed -- never frame.data pixels -- so the frame source
    # can letterbox on RGA into data itself (see App.model_frame).
    model_frame = "hw-direct"

    # `conf` / `iou` are auto-bound from manifest config_schema (and re-bound on
    # SIGHUP, since both are apply:"live") -- no setup/on_config_reload needed.

    def run(self):
        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            dets = postprocess(outs, x.info,
                               conf_thres=self.conf, iou_thres=self.iou)
            self.emit([E.detection(d) for d in dets], frame.pts, results=dets)


if __name__ == "__main__":
    run_app(YoloDetectorApp())
