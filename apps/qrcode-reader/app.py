#!/usr/bin/env python3
"""
qrcode-reader -- CPU-only reCamera Pro app (no NPU model).

The first-gen `qrcode-reader` C++ solution decoded QR codes on the CPU with
quirc (manifest pipeline `{"name":"quirc","path":"cpu","task":"qrcode-decode"}`,
no model). This port keeps the same shape: no model, pure OpenCV decode on the
ARM cores.

It is deliberately thin. All of frame grab / grey warm-up skip / result
publishing / FPS stats lives in the Kit base loop (`kit.app.App`). Because there
is no NPU model, this app sets `needs_model = False`, which makes the base loop
skip RknnModel + letterbox + infer and call `process_frame(frame)` instead. This
app only:
  * runs the shared `QrDecoder` on each frame (process_frame), and
  * shapes decoded codes into overlay-friendly "qrcode" events (on_results).

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
    postproc = "qrcode"
    needs_model = False          # CPU-only: base loop skips NPU model + letterbox

    def setup(self, config):
        super().setup(config or {})
        # Bundled CPU model files for the WeChatQRCode backend (the firmware's
        # slim cv2 lacks QRCodeDetector). Ignored if a QRCodeDetector build is
        # present. models/ ships inside the app package (build.py includes it).
        model_dir = os.path.join(_here, "models", "wechat")
        self._decoder = QrDecoder(model_dir=model_dir)

    def process_frame(self, frame):
        """Decode every QR code in the frame (base loop's no-model entry point)."""
        return self._decoder.decode(frame.data)

    def on_results(self, results, frame):
        """Shape each decoded code into a flat, overlay-friendly event."""
        return [
            {
                "kind": "qrcode",
                "text": r["text"],
                "quad": r["quad"],   # [[x,y]*4] corner points in original-frame px
            }
            for r in results
        ]


if __name__ == "__main__":
    run_app(QrcodeReaderApp())
