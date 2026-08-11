"""
Drowsiness / fatigue logic for the facemesh-reader app. Pure numpy, CPU-only.

Direct Python port of the first-gen SSCMA C++ modules
(facial_metrics.cpp / yawn_detector.cpp / drowsiness_detector.cpp). The 468-pt
MediaPipe FaceMesh index sets and all thresholds are carried over verbatim so
behaviour matches the reference. The only structural change: the C++ used
std::chrono::steady_clock; here every stateful window is driven by the frame
timestamp `t` (kit Frame.pts, monotonic seconds) passed in per update, so the
logic is deterministic and stream-clock aligned.

Three cooperating pieces, combined by `DrowsinessLogic`:

  * FaceMetrics.compute(landmarks) -> EAR (per eye + avg), MAR, closed/open flags
  * YawnTracker.update(mar, t)     -> is_yawning + 5-min yawn count (event-debounced)
  * DrowsinessTracker.update(ear, t, yawn_count) -> continuous-closure + PERCLOS
                                                    + composite level + state
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Sequence, Tuple

# --- MediaPipe FaceMesh 468-pt indices (6-pt approximation per region) ------- #
# Carried over verbatim from facemesh-reader/main/facial_metrics.h.
# Left eye:  outer corner, upper1, upper2, inner corner, lower2, lower1
LEFT_EYE_IDX = (33, 160, 158, 133, 153, 144)
# Right eye: outer corner, upper1, upper2, inner corner, lower2, lower1
RIGHT_EYE_IDX = (362, 385, 387, 263, 373, 380)
# Mouth (outer-lip 6-pt): left corner, upper-left, upper-mid, upper-right,
#                         right corner, lower-mid
MOUTH_IDX = (61, 39, 0, 269, 291, 17)

EAR_THRESHOLD = 0.21
MAR_THRESHOLD = 0.65


def _dist(a, b) -> float:
    dx = float(a[0]) - float(b[0])
    dy = float(a[1]) - float(b[1])
    return math.hypot(dx, dy)


@dataclass
class FaceMetrics:
    left_ear: float = 0.0
    right_ear: float = 0.0
    avg_ear: float = 0.0
    mar: float = 0.0
    eyes_closed: bool = False
    mouth_open: bool = False
    valid: bool = False


def _ear(lm, idx: Sequence[int]) -> float:
    # 6-point EAR (Soukupova & Cech 2016):
    #   EAR = (|p1-p5| + |p2-p4|) / (2 * |p0-p3|)
    p0, p1, p2, p3, p4, p5 = (lm[idx[k]] for k in range(6))
    horiz = _dist(p0, p3)
    if horiz < 1e-6:
        return 0.0
    return (_dist(p1, p5) + _dist(p2, p4)) / (2.0 * horiz)


def _mar(lm, idx: Sequence[int]) -> float:
    # 6-point MAR: vertical opening over horizontal mouth width.
    #   MAR = |upper_mid - lower_mid| / |left_corner - right_corner|
    left_corner = lm[idx[0]]
    upper_mid = lm[idx[2]]
    right_corner = lm[idx[4]]
    lower_mid = lm[idx[5]]
    horiz = _dist(left_corner, right_corner)
    if horiz < 1e-6:
        return 0.0
    return _dist(upper_mid, lower_mid) / horiz


def compute_metrics(landmarks,
                    ear_threshold: float = EAR_THRESHOLD,
                    mar_threshold: float = MAR_THRESHOLD) -> FaceMetrics:
    """landmarks: indexable of >=468 (x, y, ...) points (original-frame px)."""
    m = FaceMetrics()
    if landmarks is None or len(landmarks) < 468:
        return m
    m.left_ear = _ear(landmarks, LEFT_EYE_IDX)
    m.right_ear = _ear(landmarks, RIGHT_EYE_IDX)
    m.avg_ear = 0.5 * (m.left_ear + m.right_ear)
    m.mar = _mar(landmarks, MOUTH_IDX)
    m.eyes_closed = m.avg_ear < ear_threshold
    m.mouth_open = m.mar > mar_threshold
    m.valid = True
    return m


# --------------------------------------------------------------------------- #
# Yawn tracker (port of yawn_detector.cpp)
# --------------------------------------------------------------------------- #
@dataclass
class YawnState:
    is_yawning_now: bool = False
    yawn_count_5min: int = 0


class YawnTracker:
    """Yawn = MAR > threshold for >= consecutive_frames frames in a row.

    Each yawn instance is counted exactly once (event-debounce). Returns
    (state, event) where event is True only on the yawn-onset frame.
    """

    def __init__(self, mar_threshold: float = MAR_THRESHOLD,
                 consecutive_frames: int = 5, window_sec: float = 300.0):
        self.mar_threshold = float(mar_threshold)
        self.consecutive_frames = int(consecutive_frames)
        self.window_sec = float(window_sec)
        self._above = 0
        self._is_yawning = False
        self._events: Deque[float] = deque()

    def reset(self) -> None:
        self._above = 0
        self._is_yawning = False
        self._events.clear()

    def update(self, mar: float, t: float) -> Tuple[YawnState, bool]:
        event = False
        if mar > self.mar_threshold:
            self._above += 1
            if not self._is_yawning and self._above >= self.consecutive_frames:
                self._is_yawning = True
                event = True
                self._events.append(t)
        else:
            self._above = 0
            self._is_yawning = False

        cutoff = t - self.window_sec
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

        return YawnState(is_yawning_now=self._is_yawning,
                         yawn_count_5min=len(self._events)), event


# --------------------------------------------------------------------------- #
# Drowsiness tracker (port of drowsiness_detector.cpp)
# --------------------------------------------------------------------------- #
@dataclass
class DrowsinessConfig:
    ear_threshold: float = 0.21
    ear_continuous_sec: float = 2.0
    perclos_window_sec: float = 60.0
    perclos_warning_pct: float = 15.0
    perclos_critical_pct: float = 20.0
    alert_cooldown_sec: float = 5.0
    yawn_count_threshold: int = 3


@dataclass
class DrowsinessState:
    is_eyes_closed: bool = False
    continuous_closure_sec: float = 0.0
    perclos_pct: float = 0.0
    perclos_window_samples: int = 0
    drowsy_by_ear: bool = False
    drowsy_by_perclos: bool = False
    drowsy_by_yawn: bool = False
    drowsiness_level: float = 0.0
    state: str = "Alert"          # Alert / Tired / Drowsy / Danger
    alert_active: bool = False


class DrowsinessTracker:
    def __init__(self, cfg: DrowsinessConfig = None):
        self.cfg = cfg or DrowsinessConfig()
        self._is_closed = False
        self._closed_start = 0.0
        self._closure_sec = 0.0
        self._perclos: Deque[Tuple[float, bool]] = deque()
        self._closed_count = 0
        self._alert_active = False
        self._alert_start = 0.0

    def reset(self) -> None:
        self.__init__(self.cfg)

    def update(self, ear: float, t: float, yawn_count_5min: int) -> DrowsinessState:
        c = self.cfg

        # ---- 1. Continuous eye closure ----
        closed_now = ear < c.ear_threshold
        if closed_now:
            if not self._is_closed:
                self._closed_start = t
                self._is_closed = True
            self._closure_sec = t - self._closed_start
        else:
            self._is_closed = False
            self._closure_sec = 0.0
        drowsy_by_ear = self._is_closed and (self._closure_sec >= c.ear_continuous_sec)

        # ---- 2. PERCLOS sliding window ----
        self._perclos.append((t, closed_now))
        if closed_now:
            self._closed_count += 1
        cutoff = t - c.perclos_window_sec
        while self._perclos and self._perclos[0][0] < cutoff:
            if self._perclos[0][1]:
                self._closed_count -= 1
            self._perclos.popleft()
        perclos_pct = (100.0 * self._closed_count / len(self._perclos)
                       if self._perclos else 0.0)
        drowsy_by_perclos = perclos_pct >= c.perclos_critical_pct

        # ---- 3. Yawn-based ----
        drowsy_by_yawn = yawn_count_5min >= c.yawn_count_threshold

        # ---- 4. Composite level (2-D weighted; no head/gaze) ----
        ear_factor = min(1.0, self._closure_sec / max(0.001, c.ear_continuous_sec))
        perclos_factor = min(1.0, perclos_pct / max(0.001, c.perclos_critical_pct))
        level = 0.5 * ear_factor + 0.5 * perclos_factor
        if drowsy_by_yawn:
            level = min(1.0, level + 0.15)
        level = min(1.0, max(0.0, level))

        # ---- 5. State machine ----
        if level < 0.3:
            state = "Alert"
        elif level < 0.6:
            state = "Tired"
        elif level < 0.8:
            state = "Drowsy"
        else:
            state = "Danger"

        # ---- 6. Alert with cooldown ----
        if level >= 0.5:
            self._alert_active = True
            self._alert_start = t
        elif self._alert_active and (t - self._alert_start) > c.alert_cooldown_sec:
            self._alert_active = False

        return DrowsinessState(
            is_eyes_closed=self._is_closed,
            continuous_closure_sec=self._closure_sec,
            perclos_pct=perclos_pct,
            perclos_window_samples=len(self._perclos),
            drowsy_by_ear=drowsy_by_ear,
            drowsy_by_perclos=drowsy_by_perclos,
            drowsy_by_yawn=drowsy_by_yawn,
            drowsiness_level=level,
            state=state,
            alert_active=self._alert_active,
        )


# --------------------------------------------------------------------------- #
# Convenience façade combining the three (mirrors FacemeshPipeline phase 2)
# --------------------------------------------------------------------------- #
class DrowsinessLogic:
    """One-call-per-frame façade: landmarks -> metrics + yawn + drowsiness.

    When no valid face is present, still ticks the trackers with neutral inputs
    (MAR=0, EAR=1.0) so the PERCLOS window keeps shrinking and stale closures do
    not accumulate -- exactly as the first-gen pipeline did.
    """

    def __init__(self, drowsy_cfg: DrowsinessConfig = None,
                 mar_threshold: float = MAR_THRESHOLD,
                 yawn_consecutive_frames: int = 5,
                 yawn_window_sec: float = 300.0,
                 ear_threshold: float = EAR_THRESHOLD):
        self.ear_threshold = float(ear_threshold)
        self.mar_threshold = float(mar_threshold)
        self.yawn = YawnTracker(mar_threshold, yawn_consecutive_frames, yawn_window_sec)
        self.drowsy = DrowsinessTracker(drowsy_cfg)

    def update(self, landmarks, t: float):
        """Returns (FaceMetrics, YawnState, DrowsinessState, yawn_event:bool)."""
        m = compute_metrics(landmarks, self.ear_threshold, self.mar_threshold)
        if m.valid:
            yawn_state, yawn_event = self.yawn.update(m.mar, t)
            drowsy_state = self.drowsy.update(m.avg_ear, t, yawn_state.yawn_count_5min)
        else:
            yawn_state, yawn_event = self.yawn.update(0.0, t)
            drowsy_state = self.drowsy.update(1.0, t, yawn_state.yawn_count_5min)
        return m, yawn_state, drowsy_state, yawn_event
