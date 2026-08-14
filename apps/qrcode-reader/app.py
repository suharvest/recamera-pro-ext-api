#!/usr/bin/env python3
"""
qrcode-reader -- CPU-only reCamera Pro app (no NPU model).

The first-gen `qrcode-reader` C++ solution decoded QR codes on the CPU with
quirc (manifest pipeline `{"name":"quirc","path":"cpu","task":"qrcode-decode"}`,
no model). This port keeps the same shape: no model, pure OpenCV decode on the
ARM cores.

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1/§3). This is
the NO-MODEL variant of the shape, and the loop body is correspondingly short:

  frame -> QrDecoder.decode(frame.data)   CPU, ARM cores, stateless per frame
        -> self.emit()                    one "qrcode" event per decoded code

There is deliberately **no `self.pre()` and no infer step**: `needs_model` is
False, so the kit loads no RKNN model, `self.models` stays empty and the kit's
first-frame NPU warm-up is a no-op (`App._warm_up` returns immediately when
there is no model). Calling `self.pre()` here would letterbox the frame to 640
for nobody -- the decoder wants the ORIGINAL camera pixels, because `quad` is
published in original-frame coordinates.

Frame grab / grey warm-up skip / `--every` / hot-reload / FPS + latency stats
all still come from `self.frames()`, exactly as for the model-backed apps.

Run on device (no root / no NPU needed):

    KIT=/userdata/local/apps            # dir that CONTAINS kit/
    PYTHONPATH=$KIT python3 app.py --sink stdout
"""
import os
import sys

# Make the shared Kit package importable whether launched by appmgr or by hand.
# Add the directory that CONTAINS `kit/` to sys.path, then `import kit.*`.
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

from kit.app import App, run_app          # noqa: E402
from kit.logic.qrcode import QrDecoder    # noqa: E402


class QrcodeReaderApp(App):
    id = "qrcode-reader"
    name = "QR Code Reader"
    owns_loop = True             # explicit new shape: run() drives self.frames()
    needs_model = False          # CPU-only: no RKNN model, no letterbox, no infer

    def setup(self, config):
        super().setup(config or {})
        # Bundled CPU model files for the WeChatQRCode backend (the firmware's
        # slim cv2 lacks QRCodeDetector). Ignored if a QRCodeDetector build is
        # present. models/ ships inside the app package (build.py includes it).
        # These are Caffe files for the ARM cores -- NOT NPU models, which is
        # why they are not declared in the manifest `models[]`.
        model_dir = os.path.join(_here, "models", "wechat")
        self._decoder = QrDecoder(model_dir=model_dir)

    def run(self):
        for frame in self.frames():
            # CPU decode over the ORIGINAL camera pixels; `quad` comes back in
            # original-frame coordinates, which is what the overlay draws.
            codes = self._decoder.decode(frame.data)
            # Flat, overlay-friendly events -- the published contract the
            # /appcenter overlay reads is {kind, text, quad}.
            events = [
                {
                    "kind": "qrcode",
                    "text": r["text"],
                    "quad": r["quad"],   # [[x,y]*4] corners in original px
                }
                for r in codes
            ]
            self.emit(events, frame.pts, results=codes)


if __name__ == "__main__":
    run_app(QrcodeReaderApp())
