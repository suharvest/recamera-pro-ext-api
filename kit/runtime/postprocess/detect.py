"""
YOLOv8 / YOLO11 detection post-processing for reCamera Pro. Pure numpy.

Handles two RKNN output layouts automatically:

1. Concatenated & decoded head  -> a single tensor [1, 84, 8400]
   (4 box coords already regressed to pixel xywh + 80 class scores).
   This is what ultralytics' default ONNX export produces; the DFL
   integral is baked into the graph. We only reshape, threshold, NMS.

2. Raw multi-branch head -> several tensors, per FPN stride, split into a
   box-distribution branch ([1, 64, H, W], reg_max=16) and a class branch
   ([1, 80, H, W]). Here we perform the DFL decode ourselves:
   softmax over the 16 bins per side, expectation -> distance (l,t,r,b),
   grid decode -> xyxy.

Output: list of dicts {box:[x1,y1,x2,y2], cls:int, cls_name:str, score:float}
with boxes mapped back to the ORIGINAL image via LetterboxInfo.
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np

COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _softmax(x: np.ndarray, axis: int) -> np.ndarray:
    x = x - x.max(axis=axis, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> List[int]:
    """Standard greedy NMS on xyxy boxes. Returns kept indices."""
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thres]
    return keep


# --------------------------------------------------------------------------- #
# Layout 1: single concatenated & decoded output [1, 84, 8400]
# --------------------------------------------------------------------------- #
def _decode_concat(out: np.ndarray, conf_thres: float):
    """out: [84, 8400] or [8400, 84]. Returns (xyxy_640, scores, cls_ids)."""
    if out.shape[0] < out.shape[1]:
        out = out.T  # -> [8400, 84]
    box = out[:, :4].astype(np.float32)            # cx, cy, w, h  (640 space)
    cls = out[:, 4:].astype(np.float32)            # class scores
    # ultralytics export already applies sigmoid; guard for raw logits.
    if cls.min() < 0.0 or cls.max() > 1.0:
        cls = _sigmoid(cls)

    cls_ids = cls.argmax(1)
    scores = cls[np.arange(cls.shape[0]), cls_ids]
    m = scores >= conf_thres
    box, scores, cls_ids = box[m], scores[m], cls_ids[m]

    cx, cy, w, h = box[:, 0], box[:, 1], box[:, 2], box[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    return xyxy, scores, cls_ids


# --------------------------------------------------------------------------- #
# Layout 2: raw multi-branch head with DFL (reg_max=16)
# --------------------------------------------------------------------------- #
def _decode_dfl(outputs: Sequence[np.ndarray], conf_thres: float,
                input_size: int = 640, reg_max: int = 16, nc: int = 80):
    """
    Decode raw YOLOv8/11 head branches with explicit DFL integral.

    Accepts a flat list of tensors; pairs them into (box_dist, cls) per stride
    by channel count (64 = 4*reg_max box branch, nc = class branch).

    Performance: the DFL integral (softmax over 16 bins x 4 sides) is the
    expensive step, but the overwhelming majority of the 8400 anchors fall
    below `conf_thres` and are discarded. So we score the (cheap) class branch
    first, threshold to a handful of surviving anchors, and run the DFL decode
    ONLY on those. This is numerically identical to decoding every anchor and
    thresholding afterwards: sigmoid is monotonic (argmax / ordering preserved)
    and the DFL result for a kept anchor does not depend on any other anchor.
    """
    box_branches, cls_branches = [], []
    for o in outputs:
        o = np.asarray(o)
        c = o.shape[1] if o.ndim == 4 else o.shape[-1]
        if c == 4 * reg_max:
            box_branches.append(o)
        elif c == nc:
            cls_branches.append(o)
    box_branches.sort(key=lambda t: -t.shape[-1])   # large feature map first
    cls_branches.sort(key=lambda t: -t.shape[-1])

    all_xyxy, all_scores, all_cls = [], [], []
    proj = np.arange(reg_max, dtype=np.float32)
    for bd, cl in zip(box_branches, cls_branches):
        _, _, gh, gw = bd.shape
        stride = input_size // gh
        n = gh * gw

        # ---- 1. Cheap class scoring: max logit per anchor, then threshold ----
        cls = cl.reshape(nc, n)
        # `max(0)` (an axis-0 reduction) is ~6x cheaper than `argmax(0)` in
        # numpy on this CPU, so take the per-anchor max value first, threshold,
        # and run the (expensive) argmax ONLY on the handful of survivors.
        best = cls.max(0)                             # (N,) max value per anchor
        # Match original semantics: decide sigmoid over the WHOLE branch, but
        # only exponentiate the per-anchor maximum (not all nc*N logits).
        apply_sig = float(cls.min()) < 0.0 or float(best.max()) > 1.0
        scores = _sigmoid(best) if apply_sig else best
        keep = np.nonzero(scores >= conf_thres)[0]    # surviving anchor indices
        if keep.size == 0:
            continue
        cls_ids = cls[:, keep].argmax(0)              # argmax only on survivors

        # ---- 2. DFL decode ONLY for survivors ----
        bd_sel = bd.reshape(4, reg_max, n)[:, :, keep].astype(np.float32)  # (4,16,k)
        dist = (_softmax(bd_sel, axis=1) * proj[None, :, None]).sum(1)     # (4,k)
        # Grid cell centre for each survivor (row-major flatten: gx=col, gy=row).
        gx = (keep % gw).astype(np.float32) + 0.5
        gy = (keep // gw).astype(np.float32) + 0.5
        l, t, r, b = dist[0], dist[1], dist[2], dist[3]
        x1 = (gx - l) * stride
        y1 = (gy - t) * stride
        x2 = (gx + r) * stride
        y2 = (gy + b) * stride

        all_xyxy.append(np.stack([x1, y1, x2, y2], 1))
        all_scores.append(scores[keep])
        all_cls.append(cls_ids)

    if not all_xyxy:
        return np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,), dtype=int)
    return (np.concatenate(all_xyxy), np.concatenate(all_scores),
            np.concatenate(all_cls))


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #
def postprocess(outputs, info, conf_thres: float = 0.25, iou_thres: float = 0.45,
                input_size: int = 640, class_names: Sequence[str] = COCO80):
    """
    outputs : list of raw RKNN output tensors.
    info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
    Returns list of detection dicts, sorted by score descending.
    """
    outputs = [np.asarray(o) for o in outputs]

    # Pick the decoder based on layout.
    if len(outputs) == 1 and 84 in outputs[0].shape[-2:]:
        xyxy, scores, cls_ids = _decode_concat(outputs[0][0] if outputs[0].ndim == 3
                                               else outputs[0], conf_thres)
    elif len(outputs) == 1 and outputs[0].ndim >= 2 and \
            (len(class_names) + 4) in outputs[0].shape:
        arr = outputs[0][0] if outputs[0].ndim == 3 else outputs[0]
        xyxy, scores, cls_ids = _decode_concat(arr, conf_thres)
    else:
        xyxy, scores, cls_ids = _decode_dfl(outputs, conf_thres, input_size,
                                            nc=len(class_names))

    # Per-class NMS.
    keep_all = []
    for c in np.unique(cls_ids):
        idx = np.where(cls_ids == c)[0]
        k = nms(xyxy[idx], scores[idx], iou_thres)
        keep_all.extend(idx[k].tolist())

    # Map boxes from 640 letterboxed space back to original image.
    results = []
    for i in keep_all:
        x1, y1, x2, y2 = xyxy[i]
        x1 = (x1 - info.pad_w) / info.scale
        y1 = (y1 - info.pad_h) / info.scale
        x2 = (x2 - info.pad_w) / info.scale
        y2 = (y2 - info.pad_h) / info.scale
        x1 = float(np.clip(x1, 0, info.orig_w))
        y1 = float(np.clip(y1, 0, info.orig_h))
        x2 = float(np.clip(x2, 0, info.orig_w))
        y2 = float(np.clip(y2, 0, info.orig_h))
        cid = int(cls_ids[i])
        results.append({
            "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            "cls": cid,
            "cls_name": class_names[cid] if cid < len(class_names) else str(cid),
            "score": round(float(scores[i]), 4),
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results
