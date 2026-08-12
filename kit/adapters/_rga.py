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

ABI NOTE -- WHY WE USE wrapbuffer + AN OPAQUE STRUCT (read before editing)
-------------------------------------------------------------------------
`rga_buffer_t` grew across librga releases; on the device's librga (v1.10.5) it
is 96 bytes, and that library's `importbuffer_fd` takes an
`im_handle_param_t*`, NOT `(int fd, int size)` -- passing an int as the pointer
SIGSEGVs. It also forbids mixing an imported dma-buf *handle* with a virtual
address in the same im2d op. So this shim does NOT hand-build the struct or
import/release buffers itself. Instead it lets librga populate the struct via
the `wrapbuffer_*_t` constructors and treats `rga_buffer_t` as a 96-byte OPAQUE
blob (Python never reads its fields). The source is wrapped from the dma-buf
`fd` (`wrapbuffer_fd_t`) and the destination from a CPU virtual address
(`wrapbuffer_virtualaddr_t`) -- one consistent no-handles path.

Symbol names carry a `_t` suffix (`wrapbuffer_fd_t`,
`wrapbuffer_virtualaddr_t`, `imcvtcolor_t`): the non-suffixed spellings in the
public headers are macros expanding to these. Confirm on your device with
`strings librga.so | grep -iE 'wrapbuffer|imcvtcolor'`.

SAFETY / GRACEFUL DEGRADATION (read this before trusting the fast path)
----------------------------------------------------------------------
The RK_FORMAT_* enum values and the `_t` symbols below are transcribed from the
*public* Rockchip linux-rga headers (`im2d_api/im2d_type.h`, `include/rga.h`)
and verified against the device's librga. librga's ABI has drifted between
releases, so every failure mode is still defensive.

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
    Structure,
    c_char,
    c_int,
    c_void_p,
)
from typing import Optional

import numpy as np

# --- RK_FORMAT_* (include/rga.h). VERIFY against your device's rga.h. -------- #
RK_FORMAT_RGB_888 = 0x2 << 8          # 0x200  packed 24-bit R,G,B
RK_FORMAT_YCbCr_420_SP = 0xE << 8     # 0xe00  NV12 (Y plane + interleaved CbCr)

# --- IM_STATUS (im2d_api/im2d_type.h). SUCCESS == 1. ------------------------- #
IM_STATUS_SUCCESS = 1


# librga v1.10.5's `rga_buffer_t` is 96 bytes. We NEVER read/write its fields
# from Python -- librga's own `wrapbuffer_*_t` constructors populate it and we
# hand it straight back to `imcvtcolor_t`. Modelling it as a fixed-size opaque
# blob keeps the ctypes ABI (by-value arg and sret return) correct regardless of
# the exact field layout, which has drifted between librga releases.
_RGA_BUFFER_SIZE = 96


class _rga_buffer_t(Structure):
    """Opaque 96-byte mirror of im2d `rga_buffer_t` (librga v1.10.5).

    Deliberately field-less: the struct is only ever produced by librga's
    `wrapbuffer_fd_t` / `wrapbuffer_virtualaddr_t` and consumed by
    `imcvtcolor_t`. Treating it as an opaque blob avoids depending on a field
    layout that changes across librga versions."""

    _fields_ = [("_opaque", c_char * _RGA_BUFFER_SIZE)]


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
    checks the `imcvtcolor_t` IM_STATUS return code on every call.
    """

    def __init__(self) -> None:
        if os.environ.get("RECAMERA_RGA", "").strip() == "0":
            raise RuntimeError("RGA disabled via RECAMERA_RGA=0")
        lib = _load_librga()
        if lib is None:
            raise OSError("librga.so not found")
        # NO importbuffer_fd / releasebuffer_handle: on this librga
        # importbuffer_fd takes an im_handle_param_t* (not (int fd, int size)),
        # and mixing an imported handle with a virtual address in one op is
        # rejected. We use the no-handles wrapbuffer path instead.
        #
        # wrapbuffer_fd_t(int fd, int w, int h, int wstride, int hstride,
        #                 int format) -> rga_buffer_t  (returned by value / sret)
        lib.wrapbuffer_fd_t.restype = _rga_buffer_t
        lib.wrapbuffer_fd_t.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]
        # wrapbuffer_virtualaddr_t(void* vir_addr, int w, int h, int wstride,
        #                          int hstride, int format) -> rga_buffer_t
        lib.wrapbuffer_virtualaddr_t.restype = _rga_buffer_t
        lib.wrapbuffer_virtualaddr_t.argtypes = [
            c_void_p, c_int, c_int, c_int, c_int, c_int,
        ]
        # imcvtcolor_t(src, dst, sfmt, dfmt, mode, sync) -> IM_STATUS
        # src/dst are the 96-byte rga_buffer_t passed BY VALUE.
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
        # Destination: a tight CPU RGB buffer (wstride == width). RGA writes into
        # it via a virtual-address rga_buffer_t -- no dma-buf import for either
        # side, so there is nothing to release.
        out = np.empty((height, width, 3), dtype=np.uint8)

        # Source: wrap the borrowed NV12 dma-buf fd directly (no import). Y plane
        # stride/vstride come from the frame header (never derived from w/h).
        src = lib.wrapbuffer_fd_t(
            int(fd), width, height, y_stride, y_vstride,
            RK_FORMAT_YCbCr_420_SP,
        )
        # Destination: wrap the CPU RGB buffer's virtual address, tight (no pad).
        dst = lib.wrapbuffer_virtualaddr_t(
            out.ctypes.data_as(c_void_p), width, height, width, height,
            RK_FORMAT_RGB_888,
        )

        rc = lib.imcvtcolor_t(
            src, dst, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, 0, 1,
        )
        if rc != IM_STATUS_SUCCESS:
            raise RuntimeError("RGA imcvtcolor_t failed: IM_STATUS=%d" % rc)
        return out


def try_open() -> Optional[RgaNV12ToRGB]:
    """Return an `RgaNV12ToRGB` if librga is usable, else None (never raises)."""
    try:
        return RgaNV12ToRGB()
    except Exception:
        return None
