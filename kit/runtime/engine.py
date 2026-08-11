"""
RKNN inference engine for reCamera Pro (Rockchip RV1126B).

Thin wrapper over rknn_toolkit_lite2's RKNNLite (the native on-device runtime).
Only dependencies: rknnlite + numpy. No onnxruntime / torch.

librknnrt.so lives in /oem/usr/lib on the reCamera Pro; rknnlite dlopen's it
from /usr/lib, so a symlink is required once:
    ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so
"""
from __future__ import annotations

from typing import List

import numpy as np

from rknnlite.api import RKNNLite


class RknnModel:
    """Load an .rknn model and run inference on uint8 NHWC input."""

    def __init__(self, path: str, core_mask: int | None = None):
        self.path = path
        self._rknn = RKNNLite()

        ret = self._rknn.load_rknn(path)
        if ret != 0:
            raise RuntimeError(f"load_rknn failed for {path!r}: ret={ret}")

        # RV1126B is single-core NPU; pass core_mask only when explicitly given.
        if core_mask is not None:
            ret = self._rknn.init_runtime(core_mask=core_mask)
        else:
            ret = self._rknn.init_runtime()
        if ret != 0:
            raise RuntimeError(f"init_runtime failed for {path!r}: ret={ret}")

    def infer(self, input_uint8: np.ndarray) -> List[np.ndarray]:
        """
        Run one forward pass.

        `input_uint8` is a uint8 array shaped [1, H, W, 3] (NHWC) or [H, W, 3].
        Normalization (mean=0/std=255) is baked into the model, so feed RAW
        uint8 RGB pixels — do NOT divide by 255.

        Returns the list of raw output tensors (numpy arrays).
        """
        arr = np.asarray(input_uint8)
        if arr.ndim == 3:
            arr = np.expand_dims(arr, 0)
        if arr.dtype != np.uint8:
            arr = arr.astype(np.uint8)
        outputs = self._rknn.inference(inputs=[arr])
        return outputs

    def release(self) -> None:
        try:
            self._rknn.release()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
