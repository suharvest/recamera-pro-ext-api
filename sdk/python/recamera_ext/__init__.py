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
    "ProbeSource",
    "ProbeSample",
    "MaskControl",
    "MaskRect",
]

# ProbeSubscribeAck.subscribed_mask bits (mirror of RC_EXT_PROBE_MASK_*).
PROBE_MASK_PREPROC = 0x1
PROBE_MASK_NPU = 0x2
PROBE_MASK_POSTPROC = 0x4
PROBE_MASK_METRICS = 0x8

# TensorMeta.dtype -> numpy dtype string.
_PROBE_DTYPES = {
    0: "uint8",
    1: "int8",
    2: "uint16",
    3: "int16",
    4: "float32",
    5: "float16",
}

FOURCC_NV12 = 0x3231564E  # 'N','V','1','2' little-endian

_numpy = None


def _np():
    """Lazily import numpy on first use and cache it. numpy stays an optional
    dependency (the SDK core is pure ctypes); this raises ImportError at call
    time only if a frame/probe array is actually requested without numpy."""
    global _numpy
    if _numpy is None:
        import numpy as np

        _numpy = np
    return _numpy


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
        ("has_box", c_int),
        ("x1", c_float),
        ("y1", c_float),
        ("x2", c_float),
        ("y2", c_float),
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


class _MaskRect(Structure):
    """Mirror of rc_ext_mask_rect_t (normalized [0,1] hardware-cover rect)."""

    _fields_ = [
        ("id", c_int),
        ("x", c_float),
        ("y", c_float),
        ("w", c_float),
        ("h", c_float),
    ]


class MaskRect:
    """A normalized hardware privacy-mask rectangle. id selects the slot
    ([0,6)); x/y/w/h are [0,1] fractions of frame width/height."""

    def __init__(self, id, x, y, w, h):
        self.id = id
        self.x = x
        self.y = y
        self.w = w
        self.h = h


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


class _ProbeSample(Structure):
    """Mirror of rc_ext_probe_sample_t (natural alignment, matches the C ABI)."""

    _fields_ = [
        ("stage_id", c_char_p),
        ("seq", c_uint64),
        ("pts_us", c_uint64),
        ("payload", c_void_p),
        ("payload_len", c_size_t),
        ("flags", c_uint32),
        ("has_meta", c_int),
        ("shape", c_uint32 * 8),
        ("n_shape", c_uint32),
        ("dtype", c_uint32),
        ("layout", c_uint32),
        ("fourcc", c_uint32),
        ("width", c_uint32),
        ("height", c_uint32),
        ("stride", c_uint32),
        ("scale", c_float),
        ("zero_point", c_int),
        ("_fd", c_int),
        ("_base", c_void_p),
        ("_map_len", c_size_t),
        ("_pb", c_void_p),
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
    # Probe source (optional -- older libs may lack these symbols).
    if hasattr(lib, "rc_ext_probe_open"):
        lib.rc_ext_probe_open.restype = c_void_p
        lib.rc_ext_probe_open.argtypes = [POINTER(c_char_p), c_size_t, c_uint32, POINTER(c_int)]
        lib.rc_ext_probe_info.restype = c_int
        lib.rc_ext_probe_info.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32)]
        lib.rc_ext_probe_next.restype = c_int
        lib.rc_ext_probe_next.argtypes = [c_void_p, POINTER(_ProbeSample), c_int]
        lib.rc_ext_probe_release.restype = None
        lib.rc_ext_probe_release.argtypes = [c_void_p, POINTER(_ProbeSample)]
        lib.rc_ext_probe_close.restype = None
        lib.rc_ext_probe_close.argtypes = [c_void_p]
    # Hardware privacy-mask control (optional -- older libs may lack these).
    if hasattr(lib, "rc_ext_mask_open"):
        lib.rc_ext_mask_open.restype = c_void_p
        lib.rc_ext_mask_open.argtypes = [POINTER(c_int)]
        lib.rc_ext_mask_set.restype = c_int
        lib.rc_ext_mask_set.argtypes = [c_void_p, POINTER(_MaskRect), c_size_t, POINTER(c_int)]
        lib.rc_ext_mask_update.restype = c_int
        lib.rc_ext_mask_update.argtypes = [c_void_p, POINTER(_MaskRect)]
        lib.rc_ext_mask_clear.restype = c_int
        lib.rc_ext_mask_clear.argtypes = [c_void_p]
        lib.rc_ext_mask_query.restype = c_int
        lib.rc_ext_mask_query.argtypes = [c_void_p, POINTER(_MaskRect), c_size_t]
        lib.rc_ext_mask_close.restype = None
        lib.rc_ext_mask_close.argtypes = [c_void_p]
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


class _Handle:
    """Lifecycle mixin for an object owning a C handle in ``self._h``.

    Subclasses set ``_close_cfn`` (the lib close-function attribute name) and
    may override ``_on_close()`` for teardown that must run before the handle
    is closed. Provides ``close()`` plus the context-manager / ``__del__``
    protocol."""

    _close_cfn = None

    def _on_close(self):
        pass

    def close(self):
        self._on_close()
        h = getattr(self, "_h", None)
        if h:
            getattr(self._lib, self._close_cfn)(h)
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


class _BorrowIterator(_Handle):
    """Borrow-iterator over a C ``*_next`` / ``*_release`` pair. Each
    ``__next__`` releases the previously borrowed record before fetching the
    next one, so the wrapper is valid only for the current loop step.
    Subclasses provide the ctypes record type (``_record``), the lib function
    names (``_next_cfn`` / ``_release_cfn``), and ``_wrap()``."""

    _record = None
    _next_cfn = None
    _release_cfn = None

    def _wrap(self, cbuf):
        raise NotImplementedError

    def _release_cur(self):
        if self._cur is not None:
            getattr(self._lib, self._release_cfn)(self._h, byref(self._cur))
            self._cur = None

    def _on_close(self):
        self._release_cur()

    def __iter__(self):
        return self

    def __next__(self):
        self._release_cur()
        while True:
            cbuf = self._record()
            rc = getattr(self._lib, self._next_cfn)(self._h, byref(cbuf), self._timeout)
            if rc == 0:
                self._cur = cbuf
                return self._wrap(cbuf)
            if rc > 0:
                continue  # timeout, keep waiting for a live record
            raise StopIteration  # EOF / server error


class ResultSink(_Handle):
    """Injects detection results into rkipc via /run/recamera/result-in.sock."""

    _close_cfn = "rc_ext_result_close"

    def __init__(self, source_id, lib_path=None):
        self._lib = _load(lib_path)
        err = c_int(0)
        self._h = self._lib.rc_ext_result_open(source_id.encode(), byref(err))
        if not self._h:
            raise RuntimeError("rc_ext_result_open failed: err=%d" % err.value)
        self.source_id = source_id
        self._labels = []

    @staticmethod
    def _enc(label):
        if isinstance(label, str):
            return label.encode()
        return label or b""

    def _send(self, cfn, ArrayT, name, pts_us, items, fill_item):
        """Shared send scaffold: materialise ``items`` into a ``(ArrayT * n)``
        C array via ``fill_item(i, item, arr)``, call ``self._lib.<cfn>``, and
        raise on a non-zero rc (``name`` labels the error). ``self._labels`` is
        reset here as the common keepalive; callers with extra keepalives (e.g.
        masks, point arrays) reset those before invoking ``_send``."""
        items = list(items)
        n = len(items)
        arr = (ArrayT * n)()
        self._labels = []
        for i, it in enumerate(items):
            fill_item(i, it, arr)
        rc = getattr(self._lib, cfn)(self._h, c_uint64(pts_us), arr, c_size_t(n))
        if rc != 0:
            raise RuntimeError("%s failed: rc=%d" % (name, rc))
        return rc

    def send_detections(self, pts_us, boxes):
        """boxes: iterable of (x1, y1, x2, y2, score, label[, class_id]).

        Coordinates are normalized [0,1] (top-left x1/y1, bottom-right x2/y2, as
        a fraction of frame width/height). The OSD renderer clamps to [0,1] and
        multiplies by frame size, so pixel values collapse to a 1px box -- always
        send fractions, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, "person")."""

        def fill(i, b, arr):
            x1, y1, x2, y2, score, label = b[0], b[1], b[2], b[3], b[4], b[5]
            class_id = b[6] if len(b) > 6 else 0
            lb = self._enc(label)
            self._labels.append(lb)
            arr[i] = Box(float(x1), float(y1), float(x2), float(y2), float(score), lb, int(class_id))

        return self._send("rc_ext_result_send_detections", Box, "send_detections",
                          pts_us, boxes, fill)

    def send_classification(self, pts_us, items):
        """items: iterable of (score, class_id, label[, box]).

        The optional 4th element is a box (x1, y1, x2, y2); omit it or pass
        None to leave the entry box-less (original behaviour). A box attaches
        a source ROI to the entry (e.g. per-face attributes). When present, the
        box coordinates are normalized [0,1] (fraction of frame width/height),
        e.g. (0.30, 0.20, 0.55, 0.60)."""

        def fill(i, it, arr):
            score, class_id, label = it[0], it[1], it[2]
            lb = self._enc(label)
            self._labels.append(lb)
            c = Classification(float(score), int(class_id), lb)
            box = it[3] if len(it) > 3 else None
            if box is not None:
                c.has_box = 1
                c.x1, c.y1, c.x2, c.y2 = (float(box[0]), float(box[1]),
                                          float(box[2]), float(box[3]))
            else:
                c.has_box = 0
            arr[i] = c

        return self._send("rc_ext_result_send_classification", Classification,
                          "send_classification", pts_us, items, fill)

    def send_segmentation(self, pts_us, items):
        """items: iterable of
        (x1, y1, x2, y2, score, class_id, label, mask_bytes, mask_w, mask_h).
        The ROI box x1/y1/x2/y2 is normalized [0,1] (fraction of frame
        width/height), e.g. (0.05, 0.07, 0.62, 0.94). mask_bytes is raw
        row-major bytes (not coordinates) and may be None/empty (with
        mask_w=mask_h=0)."""
        self._masks = []

        def fill(i, it, arr):
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

        return self._send("rc_ext_result_send_segmentation", Segmentation,
                          "send_segmentation", pts_us, items, fill)

    def send_tracking(self, pts_us, items):
        """items: iterable of (x1, y1, x2, y2, score, class_id, label, track_id).

        Coordinates are normalized [0,1] (fraction of frame width/height), same
        contract as send_detections, e.g. (0.05, 0.07, 0.62, 0.94, 0.92, 0,
        "person", 7)."""

        def fill(i, it, arr):
            x1, y1, x2, y2, score, class_id, label, track_id = (
                it[0], it[1], it[2], it[3], it[4], it[5], it[6], it[7]
            )
            lb = self._enc(label)
            self._labels.append(lb)
            arr[i] = Tracking(
                float(x1), float(y1), float(x2), float(y2), float(score),
                int(class_id), lb, int(track_id),
            )

        return self._send("rc_ext_result_send_tracking", Tracking, "send_tracking",
                          pts_us, items, fill)

    def send_keypoints(self, pts_us, instances):
        """instances: iterable of dicts (or tuples) describing one object each:
            {
              "points": [(x, y, score, keypoint_id), ...],   # required
              "box": (x1, y1, x2, y2),   # optional; omit -> no object box
              "score": float, "class_id": int, "label": str,  # object-level
            }
        Both the point x/y and the optional object box x1/y1/x2/y2 are
        normalized [0,1] (fraction of frame width/height), same contract as
        send_detections. A missing "box" leaves the whole object_info group
        unset on the wire."""
        self._pt_arrays = []  # keep Point arrays alive until the C call returns

        def fill(i, inst, arr):
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

        return self._send("rc_ext_result_send_keypoints", KeypointInstance,
                          "send_keypoints", pts_us, instances, fill)


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
        np = _np()

        yptr = self._src._lib.rc_ext_frame_map(self._src._h, byref(self._c))
        if not yptr:
            raise RuntimeError("rc_ext_frame_map failed (seq=%d)" % self.seq)
        base = int(self._c._base)
        carr = (c_ubyte * self.buf_size).from_address(base)
        self._buf = np.ctypeslib.as_array(carr)
        return self._buf

    def plane_array(self, i):
        """Plane i as a zero-copy (rows, stride) uint8 view honouring vstride."""
        np = _np()

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

        np = _np()

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


class FrameSource(_BorrowIterator):
    """Zero-copy frame receiver over /run/recamera/frame.sock (spec §2.5).

    Iterating yields Frame objects; each is released automatically when the loop
    advances to the next frame or the context exits."""

    _record = _FrameBuf
    _next_cfn = "rc_ext_frame_next"
    _release_cfn = "rc_ext_frame_release"
    _close_cfn = "rc_ext_frame_close"

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

    def _wrap(self, cbuf):
        return Frame(self, cbuf)


class ProbeSample:
    """A borrowed probe sample. Valid only inside the current iteration step;
    the underlying buffer (inline copy or memfd mmap) is released when the loop
    advances or exits."""

    def __init__(self, src, csample):
        self._src = src
        self._c = csample
        sid = csample.stage_id
        self.stage_id = sid.decode() if sid else ""
        self.seq = csample.seq
        self.pts_us = csample.pts_us
        self.flags = csample.flags
        self.dropped = bool(csample.flags & 1)
        self.payload_len = int(csample.payload_len)
        self._ptr = csample.payload
        if csample.has_meta:
            self.meta = {
                "shape": [int(csample.shape[i]) for i in range(csample.n_shape)],
                "dtype": int(csample.dtype),
                "layout": int(csample.layout),
                "fourcc": int(csample.fourcc),
                "width": int(csample.width),
                "height": int(csample.height),
                "stride": int(csample.stride),
                "scale": float(csample.scale),
                "zero_point": int(csample.zero_point),
            }
        else:
            self.meta = None

    @property
    def payload(self):
        """The sample bytes (a copy). Valid only for this iteration step."""
        if not self._ptr or self.payload_len == 0:
            return b""
        return ctypes.string_at(self._ptr, self.payload_len)

    @property
    def array(self):
        """Zero-copy numpy view over the payload. When meta is present the view
        is typed/shaped by the TensorMeta (dtype + shape); otherwise a flat
        uint8 array. The view is valid only until the loop advances."""
        np = _np()

        if not self._ptr or self.payload_len == 0:
            return np.empty((0,), dtype=np.uint8)
        if self.meta is not None:
            npdt = np.dtype(_PROBE_DTYPES.get(self.meta["dtype"], "uint8"))
            count = self.payload_len // npdt.itemsize
            carr = (c_ubyte * self.payload_len).from_address(self._ptr)
            flat = np.frombuffer(carr, dtype=npdt, count=count)
            shape = self.meta["shape"]
            if shape and int(np.prod(shape)) == count:
                return flat.reshape(shape)
            return flat
        carr = (c_ubyte * self.payload_len).from_address(self._ptr)
        return np.frombuffer(carr, dtype=np.uint8)


class ProbeSource(_BorrowIterator):
    """Probe observability tap over /run/recamera/probe.sock (spec §4).

    Iterating yields ProbeSample objects; each is released automatically when
    the loop advances to the next sample or the context exits.

        from recamera_ext import ProbeSource
        with ProbeSource(stages=["metrics"]) as probe:
            for s in probe:
                print(s.stage_id, s.seq, s.payload_len)
    """

    _record = _ProbeSample
    _next_cfn = "rc_ext_probe_next"
    _release_cfn = "rc_ext_probe_release"
    _close_cfn = "rc_ext_probe_close"

    def __init__(self, stages, sample_every=1, timeout_ms=1000, lib_path=None):
        self._lib = _load(lib_path)
        if not hasattr(self._lib, "rc_ext_probe_open"):
            raise RuntimeError("librecamera_ext lacks probe support (rebuild >= 1.2.0)")
        stages = list(stages)
        if not stages:
            raise ValueError("stages must be a non-empty list of stage ids")
        arr = (c_char_p * len(stages))()
        for i, s in enumerate(stages):
            arr[i] = s.encode() if isinstance(s, str) else s
        err = c_int(0)
        self._h = self._lib.rc_ext_probe_open(arr, c_size_t(len(stages)),
                                              c_uint32(sample_every), byref(err))
        if not self._h:
            raise RuntimeError("rc_ext_probe_open failed: err=%d" % err.value)
        se, mask = c_uint32(), c_uint32()
        self._lib.rc_ext_probe_info(self._h, byref(se), byref(mask))
        self.sample_every = se.value
        self.subscribed_mask = mask.value
        self._timeout = timeout_ms
        self._cur = None  # _ProbeSample currently borrowed

    def _wrap(self, csample):
        return ProbeSample(self, csample)


class MaskControl(_Handle):
    """Hardware privacy-mask control -- a thin wrapper over rc_ext_mask_* (no
    logic of its own). Talks to rkipc's /var/tmp/rkipc control socket.

        from recamera_ext import MaskControl, MaskRect
        with MaskControl() as mc:
            mc.set([MaskRect(0, 0.1, 0.1, 0.3, 0.2)])   # create one block
            for x in drift():                            # incremental move, no flicker
                mc.update(MaskRect(0, x, 0.1, 0.3, 0.2))
    """

    _close_cfn = "rc_ext_mask_close"

    def __init__(self, lib_path=None):
        self._lib = _load(lib_path)
        if not hasattr(self._lib, "rc_ext_mask_open"):
            raise RuntimeError("librecamera_ext lacks mask support (rebuild the SDK)")
        err = c_int(0)
        self._h = self._lib.rc_ext_mask_open(byref(err))
        if not self._h:
            raise RuntimeError("rc_ext_mask_open failed: err=%d" % err.value)

    @staticmethod
    def _to_c(rect):
        return _MaskRect(int(rect.id), float(rect.x), float(rect.y),
                         float(rect.w), float(rect.h))

    def set(self, rects):
        """Full set of active blocks (list[MaskRect], <=6). Persisted. Returns
        the number of blocks actually applied."""
        rects = list(rects)
        n = len(rects)
        arr = (_MaskRect * n)() if n else None
        for i, r in enumerate(rects):
            arr[i] = self._to_c(r)
        applied = c_int(0)
        rc = self._lib.rc_ext_mask_set(self._h, arr, c_size_t(n), byref(applied))
        if rc < 0:
            raise RuntimeError("rc_ext_mask_set failed: rc=%d" % rc)
        return applied.value

    def update(self, rect):
        """Incrementally move a single block (no flicker, not persisted). The
        block must already exist. Returns 0; raises on error (caller may fall
        back to set())."""
        c = self._to_c(rect)
        rc = self._lib.rc_ext_mask_update(self._h, byref(c))
        if rc < 0:
            raise RuntimeError("rc_ext_mask_update failed: rc=%d" % rc)
        return rc

    def clear(self):
        """Clear all masks (persisted)."""
        rc = self._lib.rc_ext_mask_clear(self._h)
        if rc < 0:
            raise RuntimeError("rc_ext_mask_clear failed: rc=%d" % rc)
        return rc

    def query(self):
        """Return the current active masks as list[MaskRect]."""
        out = (_MaskRect * 6)()
        rc = self._lib.rc_ext_mask_query(self._h, out, c_size_t(6))
        if rc < 0:
            raise RuntimeError("rc_ext_mask_query failed: rc=%d" % rc)
        n = min(rc, 6)
        return [MaskRect(out[i].id, out[i].x, out[i].y, out[i].w, out[i].h)
                for i in range(n)]
