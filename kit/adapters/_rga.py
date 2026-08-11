"""
Optional RGA (Rockchip 2D raster graphic accelerator) NV12->RGB converter.

This module is a *thin, self-contained, OPTIONAL* ctypes shim over the device's
`librga.so` (im2d API). It exists so `OfficialFrameSource` can convert the
camera's NV12 dma-buf frames to packed RGB on the RV1126B's dedicated 2D
hardware -- near-zero CPU, reading the dma-buf fd directly -- instead of the
CPU/OpenCV path.

WHY A HAND-ROLLED CTYPES WRAPPER
--------------------------------
librga ships no official Python binding. Rather than pull a heavy third-party
package onto a constrained edge device, we bind the three im2d entry points we
actually need. Vendors copying this example get a complete, dependency-free
reference for talking to librga from Python.

SAFETY / GRACEFUL DEGRADATION (read this before trusting the fast path)
----------------------------------------------------------------------
The `rga_buffer_t` struct layout, the RK_FORMAT_* enum values and the
`imcvtcolor_t` symbol below are transcribed from the *public* Rockchip
linux-rga headers (`im2d_api/im2d_type.h`, `include/rga.h`). librga's ABI has
drifted between releases, and this code cannot be verified end-to-end without a
device that has both librga and the extension-API firmware (neither is
available in the build/CI environment -- see official.py "端侧验证 TODO").

Therefore every failure mode is defensive:
  * librga not present / not loadable                -> `available()` is False.
  * a required symbol is missing                     -> `available()` is False.
  * dma-buf import returns a bad handle, or
    `imcvtcolor_t` returns anything but SUCCESS, or
    any exception is raised during a conversion      -> the CALLER latches to
                                                        the OpenCV path for the
                                                        rest of the run.
Set `RECAMERA_RGA=0` in the environment to force the OpenCV path regardless of
librga presence (belt-and-braces for a mis-detected/mis-versioned librga).

If you have verified your device's librga against these definitions, you can
rely on the fast path; otherwise the example still runs correctly (just warmer)
via the OpenCV fallback.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from ctypes import (
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_uint64,
    c_void_p,
)
from typing import Optional

import numpy as np

# --- RK_FORMAT_* (include/rga.h). VERIFY against your device's rga.h. -------- #
RK_FORMAT_RGB_888 = 0x2 << 8          # 0x200  packed 24-bit R,G,B
RK_FORMAT_YCbCr_420_SP = 0xE << 8     # 0xe00  NV12 (Y plane + interleaved CbCr)

# --- IM_STATUS (im2d_api/im2d_type.h). SUCCESS == 1. ------------------------- #
IM_STATUS_SUCCESS = 1


class _im_rect(Structure):
    _fields_ = [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int)]


class _rga_buffer_t(Structure):
    """Mirror of im2d `rga_buffer_t`. Field order/type is ABI-critical; this
    matches the current public im2d_type.h. `handle` is the imported dma-buf
    handle (rga_buffer_handle_t)."""

    _fields_ = [
        ("vir_addr", c_void_p),
        ("phy_addr", c_void_p),
        ("fd", c_int),
        ("handle", c_int),           # rga_buffer_handle_t
        ("width", c_int),
        ("height", c_int),
        ("wstride", c_int),
        ("hstride", c_int),
        ("format", c_int),
        ("color_space_mode", c_int),
        ("global_alpha", c_int),
        ("rd_mode", c_int),
    ]


def _load_librga() -> Optional[ctypes.CDLL]:
    candidates = ["librga.so.2", "librga.so", "librga.so.1"]
    for cand in candidates:
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    found = ctypes.util.find_library("rga")
    if found:
        try:
            return ctypes.CDLL(found)
        except OSError:
            return None
    return None


class RgaNV12ToRGB:
    """Zero-copy NV12(dma-buf) -> packed RGB converter backed by librga.

    Construct it once (it binds the symbols); call `convert()` per frame. Any
    hardware/ABI problem is surfaced as an exception so the caller can latch to
    the software path -- this class NEVER silently returns wrong pixels: it
    checks the dma-buf handle and the IM_STATUS return code on every call.
    """

    def __init__(self) -> None:
        if os.environ.get("RECAMERA_RGA", "").strip() == "0":
            raise RuntimeError("RGA disabled via RECAMERA_RGA=0")
        lib = _load_librga()
        if lib is None:
            raise OSError("librga.so not found")
        # importbuffer_fd(int fd, int size) -> rga_buffer_handle_t (>0 on ok)
        lib.importbuffer_fd.restype = c_int
        lib.importbuffer_fd.argtypes = [c_int, c_int]
        # releasebuffer_handle(rga_buffer_handle_t) -> IM_STATUS
        lib.releasebuffer_handle.restype = c_int
        lib.releasebuffer_handle.argtypes = [c_int]
        # wrapbuffer_handle_t(handle, w, h, wstride, hstride, format) -> rga_buffer_t
        lib.wrapbuffer_handle_t.restype = _rga_buffer_t
        lib.wrapbuffer_handle_t.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]
        # imcvtcolor_t(src, dst, sfmt, dfmt, mode, sync) -> IM_STATUS
        lib.imcvtcolor_t.restype = c_int
        lib.imcvtcolor_t.argtypes = [
            _rga_buffer_t, _rga_buffer_t, c_int, c_int, c_int, c_int,
        ]
        self._lib = lib

    def convert(self, fd: int, width: int, height: int,
                y_stride: int, y_vstride: int) -> np.ndarray:
        """NV12 dma-buf (`fd`) -> a fresh, contiguous [H, W, 3] uint8 RGB array.

        `y_stride`/`y_vstride` are plane[0]'s byte stride and padded row count
        (from the frame header -- never derive them from width/height). The
        returned array is a standalone copy, safe to hold after the source frame
        is released.
        """
        lib = self._lib
        # Import the dma-buf as a source; size covers Y (vstride rows) + CbCr
        # (vstride/2 rows) at y_stride bytes/row.
        src_size = y_stride * (y_vstride + y_vstride // 2)
        src_handle = lib.importbuffer_fd(int(fd), int(src_size))
        if src_handle <= 0:
            raise RuntimeError("RGA importbuffer_fd failed (handle=%d)" % src_handle)

        # Destination: a tight CPU RGB buffer (wstride == width). RGA writes into
        # it via a hand-built virtual-address rga_buffer_t -- no dma-buf import
        # needed for MMU-mapped output, so there is nothing to release for `dst`.
        out = np.empty((height, width, 3), dtype=np.uint8)
        try:
            src = lib.wrapbuffer_handle_t(
                src_handle, width, height, y_stride, y_vstride,
                RK_FORMAT_YCbCr_420_SP,
            )
            dst = _rga_buffer_t()
            dst.vir_addr = out.ctypes.data_as(c_void_p)
            dst.fd = -1
            dst.handle = 0
            dst.width = width
            dst.height = height
            dst.wstride = width          # tight RGB, no padding
            dst.hstride = height
            dst.format = RK_FORMAT_RGB_888

            rc = lib.imcvtcolor_t(
                src, dst, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, 0, 1,
            )
            if rc != IM_STATUS_SUCCESS:
                raise RuntimeError("RGA imcvtcolor_t failed: IM_STATUS=%d" % rc)
            return out
        finally:
            try:
                lib.releasebuffer_handle(src_handle)
            except Exception:
                pass


def try_open() -> Optional[RgaNV12ToRGB]:
    """Return an `RgaNV12ToRGB` if librga is usable, else None (never raises)."""
    try:
        return RgaNV12ToRGB()
    except Exception:
        return None
