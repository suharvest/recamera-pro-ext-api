"""reCamera Pro extension SDK -- thin ctypes wrapper over librecamera_ext.so.1.

Two facilities, both mirroring the C ABI v1 with no logic of their own
(spec §0: "Python is a thin wrapper of the C library"):

  ResultSink  -- inject detection results       (rc_ext_result_*)
  FrameSource -- receive zero-copy camera frames (rc_ext_frame_*)

Result injection:

    from recamera_ext import ResultSink
    with ResultSink(source_id="face-app") as sink:
        sink.send_detections(pts_us=0, boxes=[(x1, y1, x2, y2, score, "label")])

Frame receiving (spec §2.5 "5 lines to the first frame"):

    from recamera_ext import FrameSource
    with FrameSource() as src:
        for frame in src:            # frame.array: zero-copy np.ndarray (Y plane)
            infer(frame.array)        # released automatically on the next iteration
"""

import ctypes
import ctypes.util
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_float,
    c_int,
    c_size_t,
    c_ubyte,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)

__all__ = [
    "ResultSink",
    "Box",
    "Classification",
    "Segmentation",
    "Tracking",
    "Point",
    "KeypointInstance",
    "FrameSource",
    "Frame",
    "FrameConfig",
]

FOURCC_NV12 = 0x3231564E  # 'N','V','1','2' little-endian


class Box(Structure):
    """Mirror of rc_ext_box_t."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("label", c_char_p),
        ("class_id", c_int),
    ]


class Classification(Structure):
    """Mirror of rc_ext_class_t."""

    _fields_ = [
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
    ]


class Segmentation(Structure):
    """Mirror of rc_ext_seg_t (ROI box + row-major mask)."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("mask", c_char_p),
        ("mask_w", c_int),
        ("mask_h", c_int),
    ]


class Tracking(Structure):
    """Mirror of rc_ext_track_t."""

    _fields_ = [
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("track_id", c_int),
    ]


class Point(Structure):
    """Mirror of rc_ext_point_t (a single keypoint)."""

    _fields_ = [
        ("x", c_float),
        ("y", c_float),
        ("score", c_float),
        ("keypoint_id", c_int),
    ]


class KeypointInstance(Structure):
    """Mirror of rc_ext_kpinstance_t (one detected object + its keypoints)."""

    _fields_ = [
        ("has_box", c_int),
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
        ("score", c_float),
        ("class_id", c_int),
        ("label", c_char_p),
        ("points", POINTER(Point)),
        ("n_points", c_size_t),
    ]


class _Plane(Structure):
    _fields_ = [("offset", c_uint32), ("stride", c_uint32), ("vstride", c_uint32)]


class _FrameBuf(Structure):
    """Mirror of rc_ext_frame_buf_t (natural alignment, matches the C ABI)."""

    _fields_ = [
        ("seq", c_uint64),
        ("pts_us", c_uint64),
        ("width", c_uint32),
        ("height", c_uint32),
        ("fourcc", c_uint32),
        ("buf_size", c_uint32),
        ("flags", c_uint16),
        ("chn_id", c_uint8),
        ("n_planes", c_uint8),
        ("plane", _Plane * 3),
        ("fd", c_int),
        ("_base", c_void_p),
        ("_map_len", c_size_t),
    ]


class _Cfg(Structure):
    _fields_ = [
        ("width", c_uint32),
        ("height", c_uint32),
        ("fourcc", c_uint32),
        ("fps_divisor", c_uint32),
    ]


class FrameConfig:
    """Optional subscription config; omit for the NPU-matched defaults."""

    def __init__(self, width=0, height=0, fourcc=0, fps_divisor=0):
        self.width = width
        self.height = height
        self.fourcc = fourcc
        self.fps_divisor = fps_divisor


def _bind(lib):
    # Result sink.
    lib.rc_ext_result_open.restype = c_void_p
    lib.rc_ext_result_open.argtypes = [c_char_p, POINTER(c_int)]
    lib.rc_ext_result_send_detections.restype = c_int
    lib.rc_ext_result_send_detections.argtypes = [c_void_p, c_uint64, POINTER(Box), c_size_t]
    lib.rc_ext_result_send_classification.restype = c_int
    lib.rc_ext_result_send_classification.argtypes = [c_void_p, c_uint64, POINTER(Classification), c_size_t]
    lib.rc_ext_result_send_segmentation.restype = c_int
    lib.rc_ext_result_send_segmentation.argtypes = [c_void_p, c_uint64, POINTER(Segmentation), c_size_t]
    lib.rc_ext_result_send_tracking.restype = c_int
    lib.rc_ext_result_send_tracking.argtypes = [c_void_p, c_uint64, POINTER(Tracking), c_size_t]
    lib.rc_ext_result_send_keypoints.restype = c_int
    lib.rc_ext_result_send_keypoints.argtypes = [c_void_p, c_uint64, POINTER(KeypointInstance), c_size_t]
    lib.rc_ext_result_close.restype = None
    lib.rc_ext_result_close.argtypes = [c_void_p]
    # Frame source.
    lib.rc_ext_frame_open.restype = c_void_p
    lib.rc_ext_frame_open.argtypes = [POINTER(_Cfg), POINTER(c_int)]
    lib.rc_ext_frame_geometry.restype = c_int
    lib.rc_ext_frame_geometry.argtypes = [c_void_p] + [POINTER(c_uint32)] * 5
    lib.rc_ext_frame_next.restype = c_int
    lib.rc_ext_frame_next.argtypes = [c_void_p, POINTER(_FrameBuf), c_int]
    lib.rc_ext_frame_map.restype = c_void_p
    lib.rc_ext_frame_map.argtypes = [c_void_p, POINTER(_FrameBuf)]
    lib.rc_ext_frame_release.restype = None
    lib.rc_ext_frame_release.argtypes = [c_void_p, POINTER(_FrameBuf)]
    lib.rc_ext_frame_close.restype = None
    lib.rc_ext_frame_close.argtypes = [c_void_p]
    return lib


def _load(path=None):
    candidates = ([path] if path else []) + ["librecamera_ext.so.1", "librecamera_ext.so"]
    for cand in candidates:
        try:
            return _bind(ctypes.CDLL(cand))
        except OSError:
            continue
    found = ctypes.util.find_library("recamera_ext")
    if found:
        return _bind(ctypes.CDLL(found))
    raise OSError("librecamera_ext.so.1 not found")


class ResultSink:
    """Injects detection results into rkipc via /run/recamera/result-in.sock."""

    def __init__(self, source_id, lib_path=None):
        self._lib = _load(lib_path)
        err = c_int(0)
        self._h = self._lib.rc_ext_result_open(source_id.encode(), byref(err))
        if not self._h:
            raise RuntimeError("rc_ext_result_open failed: err=%d" % err.value)
        self.source_id = source_id
        self._labels = []

    def send_detections(self, pts_us, boxes):
        """boxes: iterable of (x1, y1, x2, y2, score, label[, class_id])."""
        boxes = list(boxes)
        n = len(boxes)
        arr = (Box * n)()
        self._labels = []
        for i, b in enumerate(boxes):
            x1, y1, x2, y2, score, label = b[0], b[1], b[2], b[3], b[4], b[5]
            class_id = b[6] if len(b) > 6 else 0
            lb = label.encode() if isinstance(label, str) else (label or b"")
            self._labels.append(lb)
            arr[i] = Box(float(x1), float(y1), float(x2), float(y2), float(score), lb, int(class_id))
        rc = self._lib.rc_ext_result_send_detections(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("send_detections failed: rc=%d" % rc)
        return rc

    @staticmethod
    def _enc(label):
        if isinstance(label, str):
            return label.encode()
        return label or b""

    def send_classification(self, pts_us, items):
        """items: iterable of (score, class_id, label)."""
        items = list(items)
        n = len(items)
        arr = (Classification * n)()
        self._labels = []
        for i, it in enumerate(items):
            score, class_id, label = it[0], it[1], it[2]
            lb = self._enc(label)
            self._labels.append(lb)
            arr[i] = Classification(float(score), int(class_id), lb)
        rc = self._lib.rc_ext_result_send_classification(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("send_classification failed: rc=%d" % rc)
        return rc

    def send_segmentation(self, pts_us, items):
        """items: iterable of
        (x1, y1, x2, y2, score, class_id, label, mask_bytes, mask_w, mask_h).
        mask_bytes may be None/empty (with mask_w=mask_h=0)."""
        items = list(items)
        n = len(items)
        arr = (Segmentation * n)()
        self._labels = []
        self._masks = []
        for i, it in enumerate(items):
            x1, y1, x2, y2, score, class_id, label = it[0], it[1], it[2], it[3], it[4], it[5], it[6]
            mask = it[7] if len(it) > 7 else None
            mask_w = it[8] if len(it) > 8 else 0
            mask_h = it[9] if len(it) > 9 else 0
            lb = self._enc(label)
            mb = bytes(mask) if mask else b""
            self._labels.append(lb)
            self._masks.append(mb)
            arr[i] = Segmentation(
                float(x1), float(y1), float(x2), float(y2), float(score),
                int(class_id), lb, (mb if mb else None), int(mask_w), int(mask_h),
            )
        rc = self._lib.rc_ext_result_send_segmentation(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("send_segmentation failed: rc=%d" % rc)
        return rc

    def send_tracking(self, pts_us, items):
        """items: iterable of (x1, y1, x2, y2, score, class_id, label, track_id)."""
        items = list(items)
        n = len(items)
        arr = (Tracking * n)()
        self._labels = []
        for i, it in enumerate(items):
            x1, y1, x2, y2, score, class_id, label, track_id = (
                it[0], it[1], it[2], it[3], it[4], it[5], it[6], it[7]
            )
            lb = self._enc(label)
            self._labels.append(lb)
            arr[i] = Tracking(
                float(x1), float(y1), float(x2), float(y2), float(score),
                int(class_id), lb, int(track_id),
            )
        rc = self._lib.rc_ext_result_send_tracking(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("send_tracking failed: rc=%d" % rc)
        return rc

    def send_keypoints(self, pts_us, instances):
        """instances: iterable of dicts (or tuples) describing one object each:
            {
              "points": [(x, y, score, keypoint_id), ...],   # required
              "box": (x1, y1, x2, y2),   # optional; omit -> no object box
              "score": float, "class_id": int, "label": str,  # object-level
            }
        A missing "box" leaves the whole object_info group unset on the wire."""
        instances = list(instances)
        n = len(instances)
        arr = (KeypointInstance * n)()
        self._labels = []
        self._pt_arrays = []  # keep Point arrays alive until the C call returns
        for i, inst in enumerate(instances):
            pts = list(inst.get("points", []))
            np_ = len(pts)
            parr = (Point * np_)()
            for j, p in enumerate(pts):
                px, py, pscore = p[0], p[1], p[2]
                kid = p[3] if len(p) > 3 else 0
                parr[j] = Point(float(px), float(py), float(pscore), int(kid))
            self._pt_arrays.append(parr)

            box = inst.get("box")
            lb = self._enc(inst.get("label", ""))
            self._labels.append(lb)
            ke = KeypointInstance()
            if box is not None:
                ke.has_box = 1
                ke.x1, ke.y1, ke.x2, ke.y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
                ke.score = float(inst.get("score", 0.0))
                ke.class_id = int(inst.get("class_id", 0))
                ke.label = lb
            else:
                ke.has_box = 0
                ke.label = None
            ke.points = ctypes.cast(parr, POINTER(Point)) if np_ else ctypes.cast(None, POINTER(Point))
            ke.n_points = np_
            arr[i] = ke
        rc = self._lib.rc_ext_result_send_keypoints(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("send_keypoints failed: rc=%d" % rc)
        return rc

    def close(self):
        h = getattr(self, "_h", None)
        if h:
            self._lib.rc_ext_result_close(h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Frame:
    """A borrowed camera frame. Valid only inside the current iteration step;
    the underlying dma-buf is released when the loop advances or exits."""

    def __init__(self, src, cbuf):
        self._src = src
        self._c = cbuf
        self.seq = cbuf.seq
        self.pts_us = cbuf.pts_us
        self.width = cbuf.width
        self.height = cbuf.height
        self.fourcc = cbuf.fourcc
        self.buf_size = cbuf.buf_size
        self.flags = cbuf.flags
        self.chn_id = cbuf.chn_id
        self.n_planes = cbuf.n_planes
        self.dropped = bool(cbuf.flags & 1)
        self.planes = [
            (cbuf.plane[i].offset, cbuf.plane[i].stride, cbuf.plane[i].vstride)
            for i in range(cbuf.n_planes)
        ]
        self._buf = None
        self._array = None

    def _map(self):
        """mmap + DMA_BUF_IOCTL_SYNC(START|READ); returns a 1-D uint8 view over
        the whole dma-buf (zero-copy)."""
        if self._buf is not None:
            return self._buf
        import numpy as np

        yptr = self._src._lib.rc_ext_frame_map(self._src._h, byref(self._c))
        if not yptr:
            raise RuntimeError("rc_ext_frame_map failed (seq=%d)" % self.seq)
        base = int(self._c._base)
        carr = (c_ubyte * self.buf_size).from_address(base)
        self._buf = np.ctypeslib.as_array(carr)
        return self._buf

    def plane_array(self, i):
        """Plane i as a zero-copy (rows, stride) uint8 view honouring vstride."""
        import numpy as np

        buf = self._map()
        off, stride, vstride = self.planes[i]
        rows = vstride if vstride else (self.height if i == 0 else self.height // 2)
        return np.lib.stride_tricks.as_strided(
            buf[off:], shape=(rows, stride), strides=(stride, 1)
        )

    @property
    def array(self):
        """Zero-copy Y-plane view of shape (height, width), cropped to valid
        pixels (stride padding excluded). This is what `infer()` consumes."""
        if self._array is not None:
            return self._array
        y = self.plane_array(0)
        self._array = y[: self.height, : self.width]
        return self._array

    def to_bgr(self):
        """Convenience: contiguous BGR image (copy) via cv2 NV12 conversion."""
        import cv2
        import numpy as np

        buf = self._map()
        off0, stride0, vstride0 = self.planes[0]
        off1, stride1, _ = self.planes[1]
        # Build a packed NV12 buffer (Y rows then interleaved UV rows), cropping
        # stride padding so cv2 gets a tight width.
        w, h = self.width, self.height
        nv12 = np.empty((h * 3 // 2, w), dtype=np.uint8)
        ysrc = np.lib.stride_tricks.as_strided(buf[off0:], (h, stride0), (stride0, 1))
        nv12[:h] = ysrc[:, :w]
        uvsrc = np.lib.stride_tricks.as_strided(buf[off1:], (h // 2, stride1), (stride1, 1))
        nv12[h:] = uvsrc[:, :w]
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)


class FrameSource:
    """Zero-copy frame receiver over /run/recamera/frame.sock (spec §2.5).

    Iterating yields Frame objects; each is released automatically when the loop
    advances to the next frame or the context exits."""

    def __init__(self, config=None, timeout_ms=1000, lib_path=None):
        self._lib = _load(lib_path)
        cfgp = None
        if config is not None:
            self._cfg = _Cfg(config.width, config.height, config.fourcc, config.fps_divisor)
            cfgp = byref(self._cfg)
        err = c_int(0)
        self._h = self._lib.rc_ext_frame_open(cfgp, byref(err))
        if not self._h:
            raise RuntimeError("rc_ext_frame_open failed: err=%d" % err.value)
        w, h, fcc, pd, mo = (c_uint32() for _ in range(5))
        self._lib.rc_ext_frame_geometry(self._h, byref(w), byref(h), byref(fcc), byref(pd), byref(mo))
        self.width, self.height = w.value, h.value
        self.fourcc, self.pool_depth, self.max_outstanding = fcc.value, pd.value, mo.value
        self._timeout = timeout_ms
        self._cur = None  # _FrameBuf currently borrowed

    def _release_cur(self):
        if self._cur is not None:
            self._lib.rc_ext_frame_release(self._h, byref(self._cur))
            self._cur = None

    def __iter__(self):
        return self

    def __next__(self):
        self._release_cur()
        while True:
            cbuf = _FrameBuf()
            rc = self._lib.rc_ext_frame_next(self._h, byref(cbuf), self._timeout)
            if rc == 0:
                self._cur = cbuf
                return Frame(self, cbuf)
            if rc > 0:
                continue  # timeout, keep waiting for a live frame
            raise StopIteration  # EOF / server error

    def close(self):
        self._release_cur()
        h = getattr(self, "_h", None)
        if h:
            self._lib.rc_ext_frame_close(h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
