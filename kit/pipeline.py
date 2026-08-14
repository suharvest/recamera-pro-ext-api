"""
Cascade (two-stage) pipeline scaffold for reCamera Pro. Pure numpy + PIL.

This is the shared skeleton for the "detect -> crop ROI -> second model" family
of apps (facemesh-reader, and later face-analysis / anything that runs a
per-object second network). The generic Kit base loop (kit.app.App) already
does stage-1 (letterbox 640 -> detector -> boxes in ORIGINAL-frame pixels). An
app then, inside on_results(), hands the full frame + stage-1 boxes to a
CascadePipeline which:

    for each target box (top-K by score):
        1. crop a padded SQUARE ROI around the box from the ORIGINAL frame,
           edge-padding when the square runs past the frame border,
        2. resize the ROI to the second model's input size (e.g. 192),
        3. run the second RKNN model on the raw uint8 ROI,
        4. decode its outputs and map results back to ORIGINAL-frame pixels
           via the exact crop transform (ox, oy, sx, sy).

Coordinate mapping (kept linear & exact for the integer crop actually taken):

    original_x = ox + roi_x * sx
    original_y = oy + roi_y * sy

    where (ox, oy) is the top-left of the integer square in frame pixels and
    (sx, sy) = crop_side / model_input_side.

The square-crop + center-pad + resize mirrors the first-gen C++
FacemeshPipeline::cropAndResize so landmark geometry matches the reference.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

import numpy as np

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:                        # pragma: no cover
    _HAVE_PIL = False

# roi_map = (ox, oy, sx, sy): original = (ox + roi_x*sx, oy + roi_y*sy)
RoiMap = Tuple[float, float, float, float]


def crop_square_roi(frame: np.ndarray, box: Sequence[float],
                    out_size: int, pad: float = 0.25
                    ) -> Tuple[np.ndarray, RoiMap]:
    """Cut a padded, centered SQUARE ROI around `box` and resize to out_size.

    frame : HWC uint8 RGB (original frame).
    box   : [x1,y1,x2,y2] in original-frame pixels.
    Returns (roi_uint8 [out_size,out_size,3], roi_map for coordinate mapping).
    """
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))

    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    x1 -= bw * pad
    y1 -= bh * pad
    x2 += bw * pad
    y2 += bh * pad

    # Expand the shorter side to make a square around the box center.
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    side = max(x2 - x1, y2 - y1)
    half = 0.5 * side

    ix1 = int(round(cx - half))
    iy1 = int(round(cy - half))
    iside = max(1, int(round(side)))
    ix2 = ix1 + iside
    iy2 = iy1 + iside

    # Slice the valid overlap with the frame, edge-pad the out-of-bounds margin.
    sx1, sy1 = max(0, ix1), max(0, iy1)
    sx2, sy2 = min(fw, ix2), min(fh, iy2)
    if sx2 <= sx1 or sy2 <= sy1:
        # Box entirely outside frame (shouldn't happen for real detections).
        roi = np.zeros((out_size, out_size, 3), dtype=np.uint8)
        return roi, (float(ix1), float(iy1), iside / out_size, iside / out_size)

    crop = frame[sy1:sy2, sx1:sx2]
    pad_t, pad_l = sy1 - iy1, sx1 - ix1
    pad_b, pad_r = iy2 - sy2, ix2 - sx2
    if pad_t or pad_b or pad_l or pad_r:
        crop = np.pad(
            crop,
            ((max(0, pad_t), max(0, pad_b)), (max(0, pad_l), max(0, pad_r)), (0, 0)),
            mode="edge",
        )

    ch, cw = crop.shape[:2]
    if _HAVE_PIL:
        roi = np.asarray(Image.fromarray(crop).resize((out_size, out_size),
                                                       Image.BILINEAR),
                         dtype=np.uint8)
    else:                                # nearest-neighbour fallback
        ys = (np.arange(out_size) * ch / out_size).astype(np.int64).clip(0, ch - 1)
        xs = (np.arange(out_size) * cw / out_size).astype(np.int64).clip(0, cw - 1)
        roi = crop[ys][:, xs].astype(np.uint8)

    roi_map: RoiMap = (float(ix1), float(iy1), cw / out_size, ch / out_size)
    return roi, roi_map


def perspective_crop(frame: np.ndarray, quad,
                     pad_v: float = 0.12, pad_h: float = 0.06) -> np.ndarray:
    """Warp a detected text quad out of the frame into an upright text strip.

    Port of the first-gen C++ OcrPipeline::cropTextRegion. `quad` is 4 points
    ordered TL,TR,BR,BL in ORIGINAL-frame pixels. Returns an HWC uint8 RGB crop
    at the quad's natural size (rotated upright if it reads vertical). Feed the
    result to `fit_rec_input` before the rec model.

    cv2 (getPerspectiveTransform/warpPerspective) is used; it is present on the
    device system python (opencv 4.6.0) so no extra dependency is bundled.
    """
    import cv2

    src = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    fh, fw = frame.shape[:2]

    width_top = np.hypot(*(src[1] - src[0]))
    width_bot = np.hypot(*(src[2] - src[3]))
    height_l = np.hypot(*(src[3] - src[0]))
    height_r = np.hypot(*(src[2] - src[1]))
    out_w = int(max(width_top, width_bot))
    out_h = int(max(height_l, height_r))
    if out_w < 2 or out_h < 2:
        return np.zeros((2, 2, 3), dtype=np.uint8)

    # Pad the quad outward: vertical along the text-height axis, horizontal
    # along the TL->TR direction, to give the recognizer edge context.
    pv = out_h * float(pad_v)
    ph = out_w * float(pad_h)
    mid_top = (src[0] + src[1]) * 0.5
    mid_bot = (src[2] + src[3]) * 0.5
    v_axis = mid_bot - mid_top
    vl = np.hypot(*v_axis) or 1e-6
    v_axis = v_axis / vl
    h_axis = src[1] - src[0]
    hl = np.hypot(*h_axis) or 1e-6
    h_axis = h_axis / hl

    src = src.copy()
    src[0] += -v_axis * pv - h_axis * ph
    src[1] += -v_axis * pv + h_axis * ph
    src[2] += v_axis * pv + h_axis * ph
    src[3] += v_axis * pv - h_axis * ph
    src[:, 0] = np.clip(src[:, 0], 0.0, fw - 1)
    src[:, 1] = np.clip(src[:, 1], 0.0, fh - 1)
    out_w = int(out_w + 2 * ph)
    out_h = int(out_h + 2 * pv)
    if out_w < 2 or out_h < 2:
        return np.zeros((2, 2, 3), dtype=np.uint8)

    dst = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]],
                   dtype=np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(frame, m, (out_w, out_h),
                                 flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_REPLICATE)
    # Upright a vertical text region (taller than ~1.5x wide).
    if out_h > out_w * 1.5:
        warped = cv2.rotate(warped, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return np.ascontiguousarray(warped, dtype=np.uint8)


def fit_rec_input(crop: np.ndarray, out_h: int = 48, out_w: int = 320,
                  pad_value: int = 128) -> np.ndarray:
    """Resize a text crop to the rec model input (48x320), PP-OCR style.

    Scale to height `out_h` keeping aspect ratio, clamp width to `out_w`, then
    right-pad with gray (`pad_value`, maps to ~0 after the baked [-1,1] norm).
    Returns HWC uint8 RGB [out_h, out_w, 3]. Port of TextRecognizer::preprocess.
    """
    h, w = crop.shape[:2]
    if h < 1 or w < 1:
        return np.full((out_h, out_w, 3), pad_value, dtype=np.uint8)
    new_w = int(round(w * (out_h / float(h))))
    new_w = max(1, min(new_w, out_w))

    if _HAVE_PIL:
        resized = np.asarray(
            Image.fromarray(crop).resize((new_w, out_h), Image.BILINEAR),
            dtype=np.uint8)
    else:                                    # nearest-neighbour fallback
        ys = (np.arange(out_h) * h / out_h).astype(np.int64).clip(0, h - 1)
        xs = (np.arange(new_w) * w / new_w).astype(np.int64).clip(0, w - 1)
        resized = crop[ys][:, xs].astype(np.uint8)

    canvas = np.full((out_h, out_w, 3), pad_value, dtype=np.uint8)
    canvas[:, :new_w] = resized
    return canvas


class CascadePipeline:
    """Stage-2 model runner: crop ROI per stage-1 box, infer, decode, map back.

    decode_fn(outputs, roi_map, input_size) -> app-defined decoded object
    (e.g. kit.runtime.postprocess.landmark.decode returns (landmarks, presence)).
    Kept model-agnostic so face-analysis can reuse the same scaffold with a
    different second model + decode_fn.
    """

    def __init__(self, model_path: Optional[str] = None, input_size: int = 192,
                 decode_fn: Optional[Callable] = None, pad: float = 0.25,
                 max_targets: int = 1, model: Any = None):
        """`model_path` loads its own RKNN; `model` adopts an ALREADY-loaded one.

        The new app shape (KIT_APP_SHAPE_SPEC §2) preloads every manifest
        `models[]` entry into `App.models`, so a migrated cascade app passes
        `model=self.models.<id>` and this class never loads (nor releases) a
        second copy of the same rknn. Exactly one of the two must be given.
        """
        if (model is None) == (model_path is None):
            raise ValueError("CascadePipeline: pass exactly one of "
                             "model_path=... or model=<preloaded handle>")
        if model is not None:
            self.model = model
            self._owns_model = False
        else:
            # Lazy import: rknnlite only exists on-device, keep this module
            # importable off-device (unit tests, packaging) where only
            # crop_square_roi is used.
            from kit.runtime.engine import RknnModel
            self.model = RknnModel(model_path)
            self._owns_model = True
        self.input_size = int(input_size)
        self.decode_fn = decode_fn
        self.pad = float(pad)
        self.max_targets = int(max_targets)

    def process(self, frame_data: np.ndarray,
                detections: List[dict]) -> List[dict]:
        """Run stage-2 on the top-`max_targets` detections.

        Returns a list of {"box","score","decoded","roi_map"} dicts, one per
        processed detection (detections are assumed already score-sorted).
        """
        out: List[dict] = []
        for det in detections[: self.max_targets]:
            roi, roi_map = crop_square_roi(frame_data, det["box"],
                                           self.input_size, self.pad)
            outs = self.model.infer(roi)
            decoded = self.decode_fn(outs, roi_map, self.input_size)
            out.append({
                "box": det["box"],
                "score": det.get("score"),
                "decoded": decoded,
                "roi_map": roi_map,
            })
        return out

    def release(self) -> None:
        """Release the stage-2 model -- only if this pipeline loaded it itself.

        A pipeline built with `model=<App.models handle>` does NOT own the
        model; `App.finish()` releases it once, and releasing here too would be
        a double free.
        """
        if self._owns_model:
            self.model.release()
