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

import json
import multiprocessing
import os
import struct
import sys
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

_WECHAT_FILES = ("detect.prototxt", "detect.caffemodel",
                 "sr.prototxt", "sr.caffemodel")

# -- crash isolation ------------------------------------------------------- #
# `cv2.wechat_qrcode` segfaults the WHOLE process at random, inside the ZXing
# decoder bundled in libopencv_wechat_qrcode.so. Verified on device (RV1126B,
# cv2 4.6.0) -- Python faulthandler points at `detectAndDecode`, and the core
# dump's native backtrace is 11 stripped frames under
# `cv::wechat_qrcode::WeChatQRCode::Impl::decode` with NO libopencv_dnn frame,
# i.e. the ZXing decode of a candidate crop, not the super-resolution net:
#
#   #3 ..#13  ?? () from /usr/lib64/libopencv_wechat_qrcode.so.406
#   #14 cv::wechat_qrcode::WeChatQRCode::Impl::decode(...)
#   #15 cv::wechat_qrcode::WeChatQRCode::detectAndDecode(...)
#
# This is upstream opencv_contrib behaviour (issues #3150 / #3314 / #3459 /
# #3570 / #4058, all "SIGSEGV after a while"), not something this file can fix,
# and the firmware's slim cv2 does NOT ship `cv2.QRCodeDetector` to switch to.
# Mean time to crash is a few hundred to a few thousand frames -- unusable for
# a daemon that is supposed to run forever.
#
# So run the native call in a forked child process and treat its death as an
# empty result: the app survives, the child is respawned on the next frame.
# Set QR_ISOLATE=0 to force the old in-process call (debugging only).
_ISOLATE_DEFAULT = os.environ.get("QR_ISOLATE", "1") != "0"
_WORKER_TIMEOUT = float(os.environ.get("QR_WORKER_TIMEOUT", "10"))


def _wechat_worker(conn, paths: List[str], det=None) -> None:
    """Child process: decode frames off `conn` until the parent goes away.

    Closes every inherited fd except the pipe first. The parent forks us again
    after each crash, and by then it may already own the result-sink listening
    socket (:8124) and the ffmpeg pipe; inheriting those would keep the port
    bound after the parent exits -- exactly the symptom this fix removes.
    """
    # This process is EXPECTED to segfault, over and over. The firmware's
    # core_pattern is /data/core-%p-%e on /userdata, and a python core is
    # ~70 MB -- a few hundred crashes filled the 11 GB partition during
    # testing. Never dump a core from here.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

    keep = {0, 1, 2, conn.fileno()}
    try:
        for name in os.listdir("/proc/self/fd"):
            fd = int(name)
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except OSError:
        pass

    if det is None:
        try:
            det = cv2.wechat_qrcode.WeChatQRCode(*paths)
        except Exception:
            os._exit(3)

    while True:
        try:
            blob = conn.recv_bytes()
        except (EOFError, OSError):
            break
        if len(blob) < 12:
            break
        h, w, c = struct.unpack("<III", blob[:12])
        frame = np.frombuffer(blob, dtype=np.uint8, offset=12).reshape(h, w, c)
        out: List[Dict[str, Any]] = []
        try:
            texts, points = det.detectAndDecode(frame)
            for text, quad in zip(texts, points):
                if text:
                    out.append({"text": text, "quad": QrDecoder._quad(quad)})
        except cv2.error:
            out = []
        try:
            conn.send_bytes(json.dumps(out).encode())
        except (BrokenPipeError, OSError):
            break
    os._exit(0)


class _IsolatedWeChat:
    """Fork-per-life wrapper around `cv2.wechat_qrcode.WeChatQRCode`.

    `decode()` never raises and never lets a native SIGSEGV reach the caller:
    a dead or wedged child yields `[]` for that one frame and is replaced on
    the next. `crashes` counts how often that happened.
    """

    def __init__(self, paths: List[str], timeout: float = _WORKER_TIMEOUT):
        self._paths = list(paths)
        self._timeout = timeout
        self._ctx = multiprocessing.get_context("fork")
        self._proc = None
        self._conn = None
        self.crashes = 0
        # NOTE: do NOT build a detector here to let workers inherit it through
        # fork(). Tried on device: the child then wedges inside its first
        # `detectAndDecode` (OpenCV dnn state does not survive fork), every
        # frame hits the worker timeout and throughput collapses to ~0.6 fps.
        # Each worker constructs its own -- ~1 MB of Caffe models, paid once
        # per crash, not per frame.

    def _reap(self) -> None:
        conn, proc = self._conn, self._proc
        self._conn = self._proc = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if proc is not None:
            if proc.is_alive():
                try:
                    proc.terminate()
                except OSError:
                    pass
            proc.join(2)          # reap: no <defunct> left behind
            try:
                proc.close()
            except Exception:
                pass

    def _spawn(self) -> None:
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=_wechat_worker,
                                 args=(child, self._paths), daemon=True)
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent

    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        if self._proc is None or not self._proc.is_alive():
            self._reap()
            self._spawn()
        frame = np.ascontiguousarray(frame)
        h, w = frame.shape[:2]
        c = frame.shape[2] if frame.ndim == 3 else 1
        try:
            self._conn.send_bytes(struct.pack("<III", h, w, c)
                                  + frame.tobytes())
            if not self._conn.poll(self._timeout):
                raise TimeoutError("qr worker wedged")
            return json.loads(self._conn.recv_bytes().decode())
        except Exception:
            # Child segfaulted mid-decode (or hung): drop this frame, respawn
            # on the next one. This is the whole point of the isolation.
            self.crashes += 1
            self._reap()
            print("[qrcode] native decoder died (absorbed crash #%d), "
                  "respawning worker" % self.crashes,
                  file=sys.stderr, flush=True)
            return []

    def close(self) -> None:
        self._reap()


class QrDecoder:
    """Stateless multi-QR decoder over RGB/BGR uint8 frames.

    QR codes are monochrome, so RGB-vs-BGR channel order is irrelevant to
    decoding. Reusing one detector across frames avoids re-allocating it each
    call. `model_dir` is only consulted for the WeChatQRCode backend.
    """

    def __init__(self, model_dir: Optional[str] = None,
                 isolate: Optional[bool] = None) -> None:
        if hasattr(cv2, "QRCodeDetector"):
            self.backend = "builtin"
            self._det = cv2.QRCodeDetector()
        elif hasattr(cv2, "wechat_qrcode") and hasattr(cv2.wechat_qrcode,
                                                       "WeChatQRCode"):
            paths = self._wechat_paths(model_dir)
            if isolate is None:
                isolate = _ISOLATE_DEFAULT
            if isolate:
                # See the _IsolatedWeChat docstring: the native decoder
                # segfaults at random, so it lives in a child process and we
                # never construct it here.
                self.backend = "wechat-isolated"
                self._det = _IsolatedWeChat(paths)
            else:
                self.backend = "wechat"
                self._det = cv2.wechat_qrcode.WeChatQRCode(*paths)
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
        if self.backend == "wechat-isolated":
            return self._det.decode(frame)
        return self._decode_wechat(frame)

    @property
    def crashes(self) -> int:
        """How many native decoder crashes were absorbed (isolated backend)."""
        return getattr(self._det, "crashes", 0)

    def close(self) -> None:
        closer = getattr(self._det, "close", None)
        if closer is not None:
            closer()

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
