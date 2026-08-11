"""
qrcode.py -- CPU QR-code decoder (reCamera Pro logic lib).

Pure OpenCV + numpy, no NPU / no extra Python dependency. Decodes several QR
codes from one frame (mirrors the first-gen `qrcode-reader` C++ app, which used
quirc to decode multiple codes per frame). Stateless per frame, cheap enough to
run on the ARM cores every frame.

Two cv2 backends, auto-selected at construction:

  * ``cv2.QRCodeDetector`` (upstream objdetect) when the build exposes it --
    ``detectAndDecodeMulti`` handles multiple codes, no model files needed.
  * ``cv2.wechat_qrcode.WeChatQRCode`` (opencv_contrib) otherwise. The reCamera
    Pro firmware's slim cv2 4.6.0 ships ONLY this one (no QRCodeDetector), so it
    is the path used on device. It needs four small CPU (Caffe) model files --
    ``detect.prototxt / detect.caffemodel / sr.prototxt / sr.caffemodel`` -- in
    ``model_dir``. These are NOT NPU models; they run on the ARM cores. (Calling
    the WeChatQRCode empty constructor segfaults the firmware build, so real
    model paths are mandatory.)

Output shape (one dict per successfully decoded, non-empty code):
    {"text": <decoded string>, "quad": [[x,y], [x,y], [x,y], [x,y]]}
`quad` are the four corner points (integer pixels, original frame coords),
suitable for drawing an overlay polygon.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

_WECHAT_FILES = ("detect.prototxt", "detect.caffemodel",
                 "sr.prototxt", "sr.caffemodel")


class QrDecoder:
    """Stateless multi-QR decoder over RGB/BGR uint8 frames.

    QR codes are monochrome, so RGB-vs-BGR channel order is irrelevant to
    decoding. Reusing one detector across frames avoids re-allocating it each
    call. `model_dir` is only consulted for the WeChatQRCode backend.
    """

    def __init__(self, model_dir: Optional[str] = None) -> None:
        if hasattr(cv2, "QRCodeDetector"):
            self.backend = "builtin"
            self._det = cv2.QRCodeDetector()
        elif hasattr(cv2, "wechat_qrcode") and hasattr(cv2.wechat_qrcode,
                                                       "WeChatQRCode"):
            self.backend = "wechat"
            self._det = cv2.wechat_qrcode.WeChatQRCode(
                *self._wechat_paths(model_dir))
        else:
            raise RuntimeError(
                "cv2 has no QR backend (need QRCodeDetector or wechat_qrcode)")

    @staticmethod
    def _wechat_paths(model_dir: Optional[str]) -> List[str]:
        if not model_dir:
            raise RuntimeError(
                "wechat_qrcode backend requires model_dir with "
                + ", ".join(_WECHAT_FILES))
        paths = [os.path.join(model_dir, n) for n in _WECHAT_FILES]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(f"missing WeChat QR model files: {missing}")
        return paths

    # -- decode ----------------------------------------------------------- #
    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        if self.backend == "builtin":
            return self._decode_builtin(frame)
        return self._decode_wechat(frame)

    def _decode_builtin(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        try:
            ok, infos, points, _straight = self._det.detectAndDecodeMulti(frame)
        except cv2.error:
            return []
        if not ok or points is None:
            return []
        out: List[Dict[str, Any]] = []
        for text, quad in zip(infos, points):
            if not text:
                continue
            out.append({"text": text, "quad": self._quad(quad)})
        return out

    def _decode_wechat(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        try:
            texts, points = self._det.detectAndDecode(frame)
        except cv2.error:
            return []
        out: List[Dict[str, Any]] = []
        for text, quad in zip(texts, points):
            if not text:
                continue
            out.append({"text": text, "quad": self._quad(quad)})
        return out

    @staticmethod
    def _quad(quad: Any) -> List[List[int]]:
        pts = np.asarray(quad, dtype=float).reshape(-1, 2)
        return [[int(round(x)), int(round(y))] for x, y in pts]
