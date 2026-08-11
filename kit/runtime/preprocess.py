"""
Preprocessing for reCamera Pro (RV1126B) YOLO detection kit.

Pure numpy + PIL (no OpenCV). Because normalization (mean=0, std=255) is baked
into the RKNN model at convert time, this module returns RAW uint8 RGB pixels
letterboxed to the network input size. DO NOT divide by 255 here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

try:
    from PIL import Image, ImageFile
    # The on-device libjpeg is strict and rejects some COCO JPEGs as
    # "broken data stream". Allow decoding of technically-truncated files.
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


@dataclass
class LetterboxInfo:
    """Parameters needed to map boxes from network space back to the original image."""
    scale: float          # resize ratio applied to the original image
    pad_w: float          # left/right padding added (pixels, in network space)
    pad_h: float          # top/bottom padding added (pixels, in network space)
    orig_w: int
    orig_h: int


def load_image(path: str) -> np.ndarray:
    """Load an image file as an HWC uint8 RGB numpy array."""
    if not _HAVE_PIL:
        raise RuntimeError("PIL not available; cannot load image from disk")
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def letterbox(
    img: np.ndarray,
    new_shape: int | Tuple[int, int] = 640,
    color: int = 114,
) -> Tuple[np.ndarray, LetterboxInfo]:
    """
    Resize + pad an HWC uint8 RGB image to `new_shape`, preserving aspect ratio.

    Returns (padded_uint8_HWC, LetterboxInfo). Pure numpy nearest/bilinear-free
    resize via PIL when available (higher quality), else numpy nearest-neighbour.
    """
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    net_h, net_w = new_shape
    h0, w0 = img.shape[:2]

    scale = min(net_w / w0, net_h / h0)
    new_w, new_h = int(round(w0 * scale)), int(round(h0 * scale))

    if _HAVE_PIL:
        resized = np.asarray(
            Image.fromarray(img).resize((new_w, new_h), Image.BILINEAR),
            dtype=np.uint8,
        )
    else:
        # numpy nearest-neighbour fallback
        ys = (np.arange(new_h) / scale).astype(np.int64).clip(0, h0 - 1)
        xs = (np.arange(new_w) / scale).astype(np.int64).clip(0, w0 - 1)
        resized = img[ys][:, xs]

    canvas = np.full((net_h, net_w, 3), color, dtype=np.uint8)
    pad_w = (net_w - new_w) / 2.0
    pad_h = (net_h - new_h) / 2.0
    top, left = int(round(pad_h - 0.1)), int(round(pad_w - 0.1))
    canvas[top:top + new_h, left:left + new_w] = resized

    info = LetterboxInfo(scale=scale, pad_w=left, pad_h=top, orig_w=w0, orig_h=h0)
    return canvas, info


def preprocess(path_or_array, new_shape: int | Tuple[int, int] = 640):
    """
    Convenience: load (if a path) + letterbox.

    Returns (input_uint8_1HWC, LetterboxInfo). The array is shaped [1, H, W, 3]
    ready to hand to RknnModel.infer().
    """
    if isinstance(path_or_array, str):
        img = load_image(path_or_array)
    else:
        img = np.asarray(path_or_array, dtype=np.uint8)
    padded, info = letterbox(img, new_shape)
    return np.expand_dims(padded, 0), info
