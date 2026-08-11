"""
FaceMesh 468-point landmark post-processing for reCamera Pro. Pure numpy.

Second stage of the face cascade: the face_landmark model runs on a 192x192
face ROI and emits

    out0 : (1,1,1,1404)  -> 468 landmarks, each (x, y, z)
    out1 : (1,1,1,1)     -> face-presence logit

MediaPipe FaceMesh convention (matches the first-gen C++ port): landmark x, y
are in INPUT-IMAGE pixel space (0..192 for a 192 input), z is a relative depth
in the same scale. This module reshapes the flat 1404 tensor, then maps x, y
from ROI/192 space back to ORIGINAL-frame pixels using the crop transform the
pipeline recorded when it cut the ROI (see kit.pipeline.crop_square_roi).

    orig_x = roi_ox + lm_x * roi_sx
    orig_y = roi_oy + lm_y * roi_sy

Output: (landmarks_xyz float32 [468,3] in original-frame px, presence float).
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np

N_LMK = 468


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def _find_tensors(outputs: Sequence[np.ndarray]) -> Tuple[np.ndarray, Optional[float]]:
    """Return (flat 1404 landmark vector, presence scalar or None)."""
    lm = None
    presence = None
    biggest = None
    for o in outputs:
        flat = np.asarray(o).reshape(-1)
        if flat.size == N_LMK * 3:
            lm = flat
        elif flat.size == 1:
            presence = float(flat[0])
        if biggest is None or flat.size > biggest.size:
            biggest = flat
    if lm is None:                       # fallback: largest tensor
        lm = biggest
    return lm, presence


def decode(outputs, roi_map, input_size: int = 192):
    """Decode raw landmark tensors into original-frame coordinates.

    outputs  : list of raw RKNN output tensors.
    roi_map  : (ox, oy, sx, sy) crop transform from kit.pipeline.crop_square_roi
               such that original = (ox + lm_x*sx, oy + lm_y*sy).
    input_size : landmark model input side (192).

    Returns (landmarks float32 [468,3], presence float in [0,1]).
    """
    lm, presence = _find_tensors(outputs)
    pts = np.asarray(lm, dtype=np.float32).reshape(-1, 3)   # (468,3)

    xy = pts[:, :2]
    # Robustness: some exports emit normalized [0,1] coords instead of pixels.
    finite = xy[np.isfinite(xy)]
    if finite.size and float(np.abs(finite).max()) <= 4.0:
        xy = xy * float(input_size)

    ox, oy, sx, sy = roi_map
    out = np.empty_like(pts)
    out[:, 0] = ox + xy[:, 0] * sx
    out[:, 1] = oy + xy[:, 1] * sy
    out[:, 2] = pts[:, 2] * ((sx + sy) * 0.5)   # z scaled to frame px (approx)

    if presence is None:
        pres = 1.0
    elif presence < 0.0 or presence > 1.0:
        pres = float(_sigmoid(presence))
    else:
        pres = float(presence)
    return out, pres
