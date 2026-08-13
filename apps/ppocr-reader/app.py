#!/usr/bin/env python3
"""
ppocr-reader -- reCamera Pro on-device OCR (port of the first-gen SSCMA
solutions/ppocr-reader C++ pipeline to the Python kit).

Two-stage cascade:

  live frame -> letterbox 480 -> DBNet det RKNN (1,1,480,480 prob map)
     (stage 1: run by the generic Kit base loop; App.input_size=480)
  -> run_postproc(): kit.runtime.postprocess.db_ocr.decode
        threshold -> contours -> minAreaRect -> unclip -> map to orig px
        -> text-box quads (results[])
  -> on_results(): for each text box,
        kit.pipeline.perspective_crop  (warp quad -> upright strip)
        kit.pipeline.fit_rec_input     (resize/pad -> 48x320)
        rec RKNN (1,40,6625 CTC logits)
        kit.runtime.postprocess.ctc.decode (greedy CTC -> string + conf)
     -> attach recognized text to each box + emit one text event per box.

Normalization is baked into both rknns (det: ImageNet, rec: [-1,1]); we feed raw
uint8 RGB. appmgr's supervisor launches `app.py --model models[0].file` (the det
model only); the rec model + dictionary are loaded here in setup() from the
manifest -- the same self-loading pattern face-analysis / facemesh-reader use.

Run on device (inference requires root):
    KIT=/userdata/local/kit
    PYTHONPATH=$KIT python3 app.py \
        --model models/ppocr_det_fp16.rknn --sink ws --port 8124
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
from kit import pipeline                                               # noqa: E402
from kit.runtime.engine import RknnModel                              # noqa: E402
from kit.runtime.postprocess import db_ocr                            # noqa: E402
from kit.runtime.postprocess import ctc                               # noqa: E402


def _quad_bbox(quad):
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return [min(xs), min(ys), max(xs), max(ys)]


class PpocrReaderApp(App):
    id = "ppocr-reader"
    name = "PP-OCR Reader"
    postproc = "db_ocr"
    input_size = 480                       # DB detector input side (not 640)
    # "hw" (not "hw-direct"): recognition perspective-crops source-resolution
    # quads out of frame.data, so originals must survive; only the 480x480
    # letterbox moves to RGA (see App.model_frame).
    model_frame = "hw"

    def setup(self, config):
        super().setup(config)
        # Config values come from the unified effective config (kit.config).
        # The manifest is still read below, but only to locate the stage-2 rec
        # model + dictionary (models[1]) -- not for config defaults.
        manifest = {}
        try:
            with open(os.path.join(_here, "manifest.json")) as f:
                manifest = json.load(f)
        except Exception as e:                                        # pragma: no cover
            print(f"[ppocr-reader] WARN could not read manifest: {e}",
                  file=sys.stderr, flush=True)

        params = {k: v for k, v in (config or {}).items() if v is not None}

        # --- DB detection thresholds ---
        self.det_thresh = float(params.get("det_thresh", 0.3))
        self.box_thresh = float(params.get("box_thresh", 0.5))
        self.unclip_ratio = float(params.get("unclip_ratio", 2.0))
        self.max_boxes = int(params.get("max_boxes", 8))
        # --- CTC recognition ---
        self.min_rec_conf = float(params.get("min_rec_conf", 0.25))

        # --- stage-2 rec model + dictionary (models[1]) --------------------- #
        rec_file, dict_file = "models/ppocr_rec_fp16.rknn", "models/ppocr_keys_v1.txt"
        for m in manifest.get("models", []):
            if m.get("role") == "stage2_rec" or m.get("task") == "recognize":
                rec_file = m.get("file", rec_file)
                dict_file = m.get("dict", dict_file)

        def _abs(p):
            return p if os.path.isabs(p) else os.path.join(_here, p)

        self.rec_model = RknnModel(_abs(rec_file))
        self.dictionary = ctc.load_dictionary(_abs(dict_file))

        print(f"[ppocr-reader] setup det_thresh={self.det_thresh} "
              f"box_thresh={self.box_thresh} unclip={self.unclip_ratio} "
              f"max_boxes={self.max_boxes} min_rec_conf={self.min_rec_conf} "
              f"rec={os.path.basename(rec_file)} "
              f"dict_classes={len(self.dictionary)} input_size={self.input_size}",
              flush=True)

    def on_config_reload(self, config):
        """★S1 live hot-reload★ (SIGHUP -> re-read config.json).

        ppocr-reader's live knobs are the DB detection thresholds and the min
        recognition confidence -- all plain attributes read per-frame in
        run_postproc / on_results. Reapply by VALUE-REPLACE only; the stage-2 rec
        model and dictionary are never reloaded (not in config_schema).
        """
        params = self._reload_params(config)
        self.config = config or {}

        self.det_thresh = self._reload_float(params, "det_thresh", self.det_thresh)
        self.box_thresh = self._reload_float(params, "box_thresh", self.box_thresh)
        self.unclip_ratio = self._reload_float(params, "unclip_ratio", self.unclip_ratio)
        self.max_boxes = self._reload_int(params, "max_boxes", self.max_boxes)
        self.min_rec_conf = self._reload_float(params, "min_rec_conf", self.min_rec_conf)
        print(f"[ppocr-reader] hot-reload det_thresh={self.det_thresh} "
              f"box_thresh={self.box_thresh} unclip={self.unclip_ratio} "
              f"max_boxes={self.max_boxes} min_rec_conf={self.min_rec_conf}",
              flush=True)

    # -- stage 1 post-process: DB probability map -> text-box quads --------- #
    def run_postproc(self, outs, info):
        boxes = db_ocr.decode(outs, info,
                              det_thresh=self.det_thresh,
                              box_thresh=self.box_thresh,
                              unclip_ratio=self.unclip_ratio,
                              max_boxes=self.max_boxes)
        # reading order: top-to-bottom, then left-to-right (20px row tolerance)
        def _key(b):
            q = b["quad"]
            top = min(p[1] for p in q)
            left = min(p[0] for p in q)
            return (round(top / 20.0), left)
        boxes.sort(key=_key)
        results = []
        for b in boxes:
            bbox = _quad_bbox(b["quad"])
            results.append({
                "kind": "text",
                "quad": b["quad"],
                "box": bbox,
                "score": float(b["score"]),
                "cls_name": "text",
                "text": "",
                "rec_conf": 0.0,
            })
        return results

    # -- stage 2 business logic: perspective-crop + CTC recognize ----------- #
    def on_results(self, results, frame):
        events = []
        for r in results:
            crop = pipeline.perspective_crop(frame.data, r["quad"])
            fit = pipeline.fit_rec_input(crop, out_h=48, out_w=320)
            outs = self.rec_model.infer(fit)
            text, conf = ctc.decode(outs, self.dictionary)
            if text and conf >= self.min_rec_conf:
                r["text"] = text
                r["rec_conf"] = round(float(conf), 4)
            else:
                r["text"] = ""
                r["rec_conf"] = round(float(conf), 4)
            events.append({
                "kind": "text",
                "box": r["box"],
                "quad": r["quad"],
                "text": r["text"],
                "score": r["score"],
                "rec_conf": r["rec_conf"],
            })

        texts = [e["text"] for e in events if e["text"]]
        if texts:
            print(f"[ppocr-reader] boxes={len(results)} "
                  f"read={' | '.join(texts)}", flush=True)
        return events


if __name__ == "__main__":
    run_app(PpocrReaderApp())
