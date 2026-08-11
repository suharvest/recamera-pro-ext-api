"""
COCO-17 keypoint semantics + geometric helpers for pose apps. Pure numpy/math.

Ported from the first-gen fall-detection C++ (main/pose.{h,cpp} + makeObservation
in main.cpp). Keep all keypoint access through the named indices below -- raw
integer indices are easy to misread when a model uses a different landmark
convention (MediaPipe 11 == left shoulder, COCO 11 == left hip).
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

# COCO-17 keypoint order (ultralytics yolo-pose output order).
NOSE = 0
LEFT_EYE = 1
RIGHT_EYE = 2
LEFT_EAR = 3
RIGHT_EAR = 4
LEFT_SHOULDER = 5
RIGHT_SHOULDER = 6
LEFT_ELBOW = 7
RIGHT_ELBOW = 8
LEFT_WRIST = 9
RIGHT_WRIST = 10
LEFT_HIP = 11
RIGHT_HIP = 12
LEFT_KNEE = 13
RIGHT_KNEE = 14
LEFT_ANKLE = 15
RIGHT_ANKLE = 16
N_KPT = 17

Point = Tuple[float, float]


def visible(kpts: Sequence[Sequence[float]], j: int, thres: float) -> bool:
    return 0 <= j < len(kpts) and kpts[j][2] >= thres


def midpoint(kpts: Sequence[Sequence[float]], a: int, b: int,
             thres: float) -> Optional[Point]:
    """Midpoint of joints a,b. If both visible -> average; if only one -> that
    one; if neither -> None. Mirrors the first-gen `midpoint()` helper."""
    va = visible(kpts, a, thres)
    vb = visible(kpts, b, thres)
    if va and vb:
        return ((kpts[a][0] + kpts[b][0]) * 0.5, (kpts[a][1] + kpts[b][1]) * 0.5)
    if va:
        return (kpts[a][0], kpts[a][1])
    if vb:
        return (kpts[b][0], kpts[b][1])
    return None


def torso_angle_deg(shoulders: Point, hips: Point) -> Optional[float]:
    """Angle of the torso away from vertical, in degrees (0 upright, 90 flat).

    atan2(|dx|, |dy|) where d = hips - shoulders. Returns None if the two
    midpoints coincide (would be a meaningless angle)."""
    dx = hips[0] - shoulders[0]
    dy = hips[1] - shoulders[1]
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    ang = math.degrees(math.atan2(abs(dx), abs(dy)))
    if not math.isfinite(ang):
        return None
    return ang


class Observation:
    """One frame's fall features for a single subject (mirrors FallObservation).

    Coordinates normalised to the inference frame; hip_y increases downward,
    torso_angle_deg is degrees from vertical, aspect = box_w / box_h.
    """
    __slots__ = ("valid", "timestamp_sec", "hip_y", "torso_angle_deg",
                 "bbox_aspect_ratio", "person_score")

    def __init__(self, timestamp_sec: float):
        self.valid = False
        self.timestamp_sec = timestamp_sec
        self.hip_y = 0.0
        self.torso_angle_deg = 0.0
        self.bbox_aspect_ratio = 0.0
        self.person_score = 0.0


def make_observation(person: Optional[dict], timestamp_sec: float,
                     frame_h: int, kpt_thres: float) -> Observation:
    """Build an Observation from a pose result dict (box + keypoints in pixels).

    `person` is one entry of kit.runtime.postprocess.pose output, or None when no
    subject was detected (-> invalid observation, lets a suspicion expire).
    `frame_h` is the ORIGINAL frame height (pixels) used to normalise hip_y.
    """
    obs = Observation(timestamp_sec)
    if person is None or frame_h <= 0:
        return obs
    box = person.get("box")
    kpts = person.get("keypoints")
    if not box or not kpts:
        return obs
    bw = box[2] - box[0]
    bh = box[3] - box[1]
    if bh <= 1e-4:
        return obs

    hips = midpoint(kpts, LEFT_HIP, RIGHT_HIP, kpt_thres)
    shoulders = midpoint(kpts, LEFT_SHOULDER, RIGHT_SHOULDER, kpt_thres)
    if hips is None or shoulders is None:
        return obs
    ang = torso_angle_deg(shoulders, hips)
    if ang is None:
        return obs

    obs.valid = True
    obs.hip_y = hips[1] / float(frame_h)
    obs.torso_angle_deg = ang
    obs.bbox_aspect_ratio = bw / bh
    obs.person_score = float(person.get("score", 0.0))
    return obs


def joint_angle(a: Optional[Point], b: Optional[Point],
                c: Optional[Point]) -> Optional[float]:
    """Interior angle at vertex b, in degrees, range [0,180].

    Ported from the first-gen fitness-trainer `jointAngle` (main/pose.cpp).
    Returns None ( == the C++ NaN) when either limb has zero length
    (coincident keypoints) or an input is missing -- callers MUST treat that as
    "no reading" rather than as 0 degrees."""
    if a is None or b is None or c is None:
        return None
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    mag_ba = math.hypot(bax, bay)
    mag_bc = math.hypot(bcx, bcy)
    if mag_ba < 1e-3 or mag_bc < 1e-3:
        return None
    cosine = (bax * bcx + bay * bcy) / (mag_ba * mag_bc)
    cosine = max(-1.0, min(1.0, cosine))          # guard acos() domain
    ang = math.degrees(math.acos(cosine))
    return ang if math.isfinite(ang) else None


def point(kpts: Sequence[Sequence[float]], j: int) -> Optional[Point]:
    """(x, y) of joint j, or None if the index is out of range."""
    if 0 <= j < len(kpts):
        return (kpts[j][0], kpts[j][1])
    return None


def side_score(kpts: Sequence[Sequence[float]], joints: Sequence[int],
               thres: float) -> float:
    """Mean confidence over `joints`; 0.0 when ANY joint is below `thres`.

    Ported from first-gen `Pose::sideScore` -- used to pick the better-facing
    side of a two-sided exercise."""
    total = 0.0
    n = 0
    for j in joints:
        if not visible(kpts, j, thres):
            return 0.0
        total += kpts[j][2]
        n += 1
    return total / n if n else 0.0


# --------------------------------------------------------------------------- #
# 2-D scene geometry (zone / line configuration). Ported from the first-gen
# retail-vision C++ `geometry.h`. All coordinates are normalised to [0,1] so the
# same zone/line survives any capture resolution. Pure math, no numpy.
# --------------------------------------------------------------------------- #
def point_in_polygon(px: float, py: float,
                     poly: Sequence[Sequence[float]]) -> bool:
    """Ray-casting (crossing-number) point-in-polygon; handles non-convex
    polygons. Points exactly on an edge may fall on either side -- acceptable
    for occupancy counting. `poly` is a sequence of (x, y). <3 points -> False.
    Faithful port of retail_vision::geom::point_in_polygon."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def line_side(ax: float, ay: float, bx: float, by: float,
              px: float, py: float) -> float:
    """Signed side of point p relative to the directed line a -> b (2-D cross
    product). > 0 : p is LEFT of a->b, < 0 : RIGHT, == 0 : collinear."""
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def segment_crossing(ax: float, ay: float, bx: float, by: float,
                     p0x: float, p0y: float,
                     p1x: float, p1y: float) -> int:
    """Did the movement segment p0 -> p1 cross the finite segment a -> b?

    Returns 0 (no crossing), +1 (crossed from the LEFT of a->b to the RIGHT),
    or -1 (RIGHT -> LEFT). Requires BOTH segments to strictly straddle each
    other, so touching an endpoint or moving parallel past the line does not
    count. Faithful port of retail_vision::geom::segment_crossing -- the sign
    convention (left->right = +1) is what LineCounter's `ab_in` keys off."""
    side0 = line_side(ax, ay, bx, by, p0x, p0y)
    side1 = line_side(ax, ay, bx, by, p1x, p1y)
    if side0 == 0.0 or side1 == 0.0 or (side0 > 0) == (side1 > 0):
        return 0
    sa = line_side(p0x, p0y, p1x, p1y, ax, ay)
    sb = line_side(p0x, p0y, p1x, p1y, bx, by)
    if sa == 0.0 or sb == 0.0 or (sa > 0) == (sb > 0):
        return 0
    return 1 if side0 > 0 else -1


def iou_xyxy(a: Sequence[float], b: Sequence[float]) -> float:
    """IoU of two axis-aligned boxes in [x1,y1,x2,y2] form (any consistent
    unit). Returns 0.0 for non-overlapping or degenerate boxes."""
    ax1, ay1, ax2, ay2 = a[0], a[1], a[2], a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[2], b[3]
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


COCO_SKELETON: List[Tuple[int, int]] = [
    (LEFT_ANKLE, LEFT_KNEE), (LEFT_KNEE, LEFT_HIP),
    (RIGHT_ANKLE, RIGHT_KNEE), (RIGHT_KNEE, RIGHT_HIP),
    (LEFT_HIP, RIGHT_HIP), (LEFT_SHOULDER, LEFT_HIP),
    (RIGHT_SHOULDER, RIGHT_HIP), (LEFT_SHOULDER, RIGHT_SHOULDER),
    (LEFT_SHOULDER, LEFT_ELBOW), (RIGHT_SHOULDER, RIGHT_ELBOW),
    (LEFT_ELBOW, LEFT_WRIST), (RIGHT_ELBOW, RIGHT_WRIST),
    (NOSE, LEFT_EYE), (NOSE, RIGHT_EYE),
    (LEFT_EYE, LEFT_EAR), (RIGHT_EYE, RIGHT_EAR),
]
