"""
Single-class YOLOv8-face detection post-processing for reCamera Pro. Pure numpy.

This is a thin specialisation of the generic YOLOv8/11 DFL decoder in
`detect.py` for the 1-class face detector (yolov8n-face rawhead):

    per FPN stride s in {8, 16, 32}:
        box branch  [1, 64, {80,40,20}^2]   -> DFL(reg_max=16) -> l,t,r,b
        cls branch  [1,  1, {80,40,20}^2]    -> face score (single class)

The rawhead export carries NO keypoints (box-only), so `detect._decode_dfl`
already handles this layout verbatim when `nc=1`: it pairs a 64-channel box
branch with a 1-channel class branch per stride and runs the same "score-first,
DFL-decode-survivors" path. We keep a dedicated entry point so the cascade
pipeline / face apps read clearly and get a face-shaped result:

    [{ "box":[x1,y1,x2,y2], "score":float }]   (boxes in ORIGINAL-image pixels)
"""
from __future__ import annotations

from typing import List

import numpy as np

from kit.runtime.postprocess.detect import _decode_dfl, nms


def postprocess(outputs, info, conf_thres: float = 0.5, iou_thres: float = 0.45,
                input_size: int = 640) -> List[dict]:
    """Decode the 6-tensor yolov8n-face rawhead into face boxes.

    outputs : list of raw RKNN tensors (3x box-DFL [1,64,g,g] + 3x cls [1,1,g,g]).
    info    : preprocess.LetterboxInfo (scale + padding for un-letterboxing).
    Returns face dicts sorted by score descending, boxes in original-frame px.
    """
    outputs = [np.asarray(o) for o in outputs]
    # nc=1 -> the class branch is the 1-channel tensor; box branch is 64-channel.
    xyxy, scores, _cls = _decode_dfl(outputs, conf_thres, input_size, nc=1)

    keep = nms(xyxy, scores, iou_thres)   # single class => global NMS

    results = []
    for i in keep:
        x1, y1, x2, y2 = xyxy[i]
        x1 = float(np.clip((x1 - info.pad_w) / info.scale, 0, info.orig_w))
        y1 = float(np.clip((y1 - info.pad_h) / info.scale, 0, info.orig_h))
        x2 = float(np.clip((x2 - info.pad_w) / info.scale, 0, info.orig_w))
        y2 = float(np.clip((y2 - info.pad_h) / info.scale, 0, info.orig_h))
        results.append({
            "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            "score": round(float(scores[i]), 4),
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results
