"""
Exercise rep counting for pose apps -- joint-angle relaxation/flexion state
machines that turn a stream of keypoints into a rep count.

Ported faithfully from the first-gen fitness-trainer C++
(solutions/fitness-trainer/main/{exercise.h,exercise.cpp,pose.cpp}). The
thresholds, hysteresis band, debounce, EMA smoothing, two-sided pairing and
"count on the way back up" behaviour are all the original's -- see exercise.h's
header comment for WHY each differs from the even-earlier Python original.

Pure math + kit.logic.geometry; no numpy, no model coupling. Everything operates
on decoded pose dicts (kit.runtime.postprocess.pose output: {box, score,
keypoints:[[x,y,conf]*17]}) so fall-detection and fitness-trainer share one
keypoint convention.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from kit.logic import geometry as G

# --------------------------------------------------------------------------- #
# Per-exercise thresholds (degrees / seconds), verbatim from exercise.cpp.
# --------------------------------------------------------------------------- #
_SQUAT_UP = 160.0        # standing (knee ~straight)
_SQUAT_DOWN = 100.0      # parallel-ish
_SQUAT_PARTIAL = 120.0   # shallower than this at the bottom = partial rep

_PUSHUP_UP = 150.0       # arms locked out
_PUSHUP_DOWN = 95.0      # chest down
_PUSHUP_PARTIAL = 110.0

_CURL_EXTENDED = 150.0   # arm hanging
_CURL_FLEXED = 50.0      # fully curled
_CURL_ELBOW_DRIFT = 40.0  # upper arm vs torso, degrees

_SMOOTHING_ALPHA = 0.5   # EMA over the raw angle
_LOST_SECONDS = 1.5      # miss() budget before the phase resets


class RepCounter:
    """Hysteresis rep counter shared by every exercise (port of C++ RepCounter).

    Phase is "extended" above up_threshold and "flexed" below down_threshold,
    with the band between them holding the previous phase -- that band is what
    stops keypoint jitter from counting reps. A rep completes on
    flexed -> extended, no sooner than `min_interval` after the last one.
    """

    _UNKNOWN, _EXTENDED, _FLEXED = 0, 1, 2

    def __init__(self, up_threshold: float, down_threshold: float,
                 min_interval: float = 0.4):
        self._up = up_threshold
        self._down = down_threshold
        self._min_interval = min_interval
        self._ever_read = False
        self.reset()

    def reset(self) -> None:
        self._phase = self._UNKNOWN
        self._has_smoothed = False
        self._smoothed = 0.0
        self._rep_min = 180.0
        self._last_rep_min = 180.0
        self._last_rep_time = -1e9
        self._last_reading_time = -1e9
        # _ever_read deliberately survives reset(): it describes the camera view
        # (was this side ever visible), not the workout.

    def miss(self, now_sec: float) -> None:
        """Feed a frame with no usable reading (joints hidden). After
        _LOST_SECONDS of these the phase resets, so an athlete who walks away
        and comes back does not resume mid-rep."""
        if self._has_smoothed and now_sec - self._last_reading_time > _LOST_SECONDS:
            self._phase = self._UNKNOWN
            self._has_smoothed = False
            self._rep_min = 180.0

    def update(self, angle: Optional[float], now_sec: float) -> bool:
        """Feed one angle reading. Returns True on the frame a rep completes.

        `angle` is None ( == C++ NaN) when the joint triplet is not readable."""
        if angle is None:
            self.miss(now_sec)
            return False

        self._ever_read = True
        self._last_reading_time = now_sec
        self._smoothed = (
            _SMOOTHING_ALPHA * angle + (1.0 - _SMOOTHING_ALPHA) * self._smoothed
            if self._has_smoothed else angle)
        self._has_smoothed = True

        if self._phase == self._FLEXED:
            self._rep_min = min(self._rep_min, self._smoothed)

        if self._smoothed < self._down:
            if self._phase != self._FLEXED:
                self._phase = self._FLEXED
                self._rep_min = self._smoothed
            return False

        if self._smoothed > self._up:
            completes = self._phase == self._FLEXED
            self._phase = self._EXTENDED
            if completes:
                # Debounce: a genuine rep cannot be faster than min_interval;
                # anything quicker is the angle rattling across both thresholds.
                if now_sec - self._last_rep_time < self._min_interval:
                    return False
                self._last_rep_time = now_sec
                self._last_rep_min = self._rep_min
                self._rep_min = 180.0
                return True
            return False

        # Inside the hysteresis band: hold the current phase.
        return False

    # -- read-only accessors (mirror the C++ getters) -------------------- #
    @property
    def smoothed(self) -> float:
        return self._smoothed

    def has_reading(self) -> bool:
        return self._has_smoothed

    def extended(self) -> bool:
        return self._phase == self._EXTENDED

    def flexed(self) -> bool:
        return self._phase == self._FLEXED

    def last_rep_min_angle(self) -> float:
        return self._last_rep_min

    def ever_read(self) -> bool:
        return self._ever_read


# --------------------------------------------------------------------------- #
# Exercise state + base class (port of ExerciseState / Exercise).
# --------------------------------------------------------------------------- #
class ExerciseState:
    """What the app reads each frame (mirror of the C++ ExerciseState struct)."""

    def __init__(self, two_sided: bool = False):
        self.reps = 0             # reps completed in the CURRENT set
        self.set = 1              # 1-based
        self.workout_complete = False
        self.tracking = False     # a usable reading came out of this frame
        self.stage = "idle"       # exercise-specific, user-facing
        self.angle = 0.0          # primary tracked angle, degrees
        self.has_angle = False
        self.two_sided = two_sided
        self.reps_left = 0
        self.reps_right = 0
        self.form_warning = ""    # empty when form is fine
        # Edge flags, true only on the frame the event happened.
        self.rep_completed = False
        self.set_completed = False

    def as_dict(self) -> Dict:
        d = {
            "reps": self.reps,
            "set": self.set,
            "workout_complete": self.workout_complete,
            "tracking": self.tracking,
            "stage": self.stage,
            "angle": round(self.angle, 1),
            "has_angle": self.has_angle,
            "rep_completed": self.rep_completed,
            "set_completed": self.set_completed,
            "form_warning": self.form_warning,
        }
        if self.two_sided:
            d["reps_left"] = self.reps_left
            d["reps_right"] = self.reps_right
        return d


class Exercise:
    """Base exercise. Subclasses implement `_track` + `_on_reset`.

    `update(person, now_sec)` advances the state machine by one frame; pass
    person == None when nobody was detected. `person` is one pose result dict
    ({box, score, keypoints}); keypoint visibility is gated by kpt_thres.
    """

    id = "exercise"
    display_name = "Exercise"

    def __init__(self, kpt_thres: float = 0.5):
        self.kpt_thres = kpt_thres
        self.target_reps = 12
        self.target_sets = 3
        self.state = ExerciseState(two_sided=self._two_sided())

    def _two_sided(self) -> bool:
        return False

    def set_targets(self, target_reps: int, target_sets: int) -> None:
        self.target_reps = max(1, int(target_reps))
        self.target_sets = max(1, int(target_sets))

    def reset(self) -> None:
        self.state = ExerciseState(two_sided=self._two_sided())
        self._on_reset()

    # subclasses override -------------------------------------------------- #
    def _on_reset(self) -> None:
        raise NotImplementedError

    def _track(self, kpts, now_sec) -> int:
        """Return number of reps completed on this frame (kpts may be None)."""
        raise NotImplementedError

    # generic per-frame drive (port of Exercise::update) ------------------- #
    def update(self, person: Optional[dict], now_sec: float) -> ExerciseState:
        s = self.state
        s.rep_completed = False
        s.set_completed = False
        s.form_warning = ""

        kpts = person.get("keypoints") if person else None
        if not kpts:
            s.tracking = False
            s.has_angle = False
            s.stage = "idle"
            self._track(None, now_sec)     # still tick counters so phase decays
            return s

        completed = self._track(kpts, now_sec)
        if completed <= 0 or s.workout_complete:
            return s

        for _ in range(completed):
            s.reps += 1
            s.rep_completed = True
            if s.reps >= self.target_reps:
                s.set_completed = True
                if s.set >= self.target_sets:
                    s.workout_complete = True
                    s.reps = self.target_reps
                    break
                s.set += 1
                s.reps = 0
        return s


# --------------------------------------------------------------------------- #
# Squat -- knee flexion (hip / knee / ankle).
# --------------------------------------------------------------------------- #
class Squat(Exercise):
    id = "squat"
    display_name = "Squat"

    def __init__(self, kpt_thres: float = 0.5):
        super().__init__(kpt_thres)
        self._counter = RepCounter(_SQUAT_UP, _SQUAT_DOWN, 0.5)

    def _on_reset(self):
        self._counter.reset()

    def _track(self, kpts, now_sec) -> int:
        s = self.state
        if kpts is None:
            self._counter.miss(now_sec)
            return 0

        t = self.kpt_thres
        left = G.side_score(kpts, (G.LEFT_HIP, G.LEFT_KNEE, G.LEFT_ANKLE), t)
        right = G.side_score(kpts, (G.RIGHT_HIP, G.RIGHT_KNEE, G.RIGHT_ANKLE), t)
        if left <= 0.0 and right <= 0.0:
            s.tracking = False
            s.has_angle = False
            s.stage = "out of frame"
            self._counter.miss(now_sec)
            return 0

        use_left = left >= right
        hip, knee, ankle = ((G.LEFT_HIP, G.LEFT_KNEE, G.LEFT_ANKLE) if use_left
                            else (G.RIGHT_HIP, G.RIGHT_KNEE, G.RIGHT_ANKLE))
        angle = G.joint_angle(G.point(kpts, hip), G.point(kpts, knee),
                              G.point(kpts, ankle))
        rep = self._counter.update(angle, now_sec)

        s.tracking = self._counter.has_reading()
        s.has_angle = s.tracking
        s.angle = self._counter.smoothed
        s.stage = "down" if self._counter.flexed() else (
            "up" if self._counter.extended() else "idle")

        if rep and self._counter.last_rep_min_angle() > _SQUAT_PARTIAL:
            s.form_warning = "Partial rep - squat deeper"
        return 1 if rep else 0


# --------------------------------------------------------------------------- #
# Push-up -- elbow flexion (shoulder / elbow / wrist).
# --------------------------------------------------------------------------- #
class PushUp(Exercise):
    id = "push_up"
    display_name = "Push-up"

    def __init__(self, kpt_thres: float = 0.5):
        super().__init__(kpt_thres)
        self._counter = RepCounter(_PUSHUP_UP, _PUSHUP_DOWN, 0.5)

    def _on_reset(self):
        self._counter.reset()

    def _track(self, kpts, now_sec) -> int:
        s = self.state
        if kpts is None:
            self._counter.miss(now_sec)
            return 0

        t = self.kpt_thres
        left = G.side_score(kpts, (G.LEFT_SHOULDER, G.LEFT_ELBOW, G.LEFT_WRIST), t)
        right = G.side_score(kpts, (G.RIGHT_SHOULDER, G.RIGHT_ELBOW, G.RIGHT_WRIST), t)
        if left <= 0.0 and right <= 0.0:
            s.tracking = False
            s.has_angle = False
            s.stage = "out of frame"
            self._counter.miss(now_sec)
            return 0

        use_left = left >= right
        sh, el, wr = ((G.LEFT_SHOULDER, G.LEFT_ELBOW, G.LEFT_WRIST) if use_left
                      else (G.RIGHT_SHOULDER, G.RIGHT_ELBOW, G.RIGHT_WRIST))
        angle = G.joint_angle(G.point(kpts, sh), G.point(kpts, el),
                              G.point(kpts, wr))
        rep = self._counter.update(angle, now_sec)

        s.tracking = self._counter.has_reading()
        s.has_angle = s.tracking
        s.angle = self._counter.smoothed
        s.stage = "down" if self._counter.flexed() else (
            "up" if self._counter.extended() else "idle")

        if rep and self._counter.last_rep_min_angle() > _PUSHUP_PARTIAL:
            s.form_warning = "Partial rep - lower your chest"
        return 1 if rep else 0


# --------------------------------------------------------------------------- #
# Hammer curl -- both arms, counted independently, paired for the set counter.
# --------------------------------------------------------------------------- #
class HammerCurl(Exercise):
    id = "hammer_curl"
    display_name = "Hammer Curl"

    def __init__(self, kpt_thres: float = 0.5):
        super().__init__(kpt_thres)
        self._left = RepCounter(_CURL_EXTENDED, _CURL_FLEXED, 0.4)
        self._right = RepCounter(_CURL_EXTENDED, _CURL_FLEXED, 0.4)
        self._paired = 0

    def _two_sided(self) -> bool:
        return True

    def _on_reset(self):
        self._left.reset()
        self._right.reset()
        self._paired = 0

    def _track(self, kpts, now_sec) -> int:
        s = self.state
        if kpts is None:
            self._left.miss(now_sec)
            self._right.miss(now_sec)
            return 0

        t = self.kpt_thres
        has_left = G.side_score(kpts, (G.LEFT_SHOULDER, G.LEFT_ELBOW, G.LEFT_WRIST), t) > 0.0
        has_right = G.side_score(kpts, (G.RIGHT_SHOULDER, G.RIGHT_ELBOW, G.RIGHT_WRIST), t) > 0.0
        if not has_left and not has_right:
            s.tracking = False
            s.has_angle = False
            s.stage = "out of frame"
            self._left.miss(now_sec)
            self._right.miss(now_sec)
            return 0

        angle_left = (G.joint_angle(G.point(kpts, G.LEFT_SHOULDER),
                                    G.point(kpts, G.LEFT_ELBOW),
                                    G.point(kpts, G.LEFT_WRIST))
                      if has_left else None)
        angle_right = (G.joint_angle(G.point(kpts, G.RIGHT_SHOULDER),
                                     G.point(kpts, G.RIGHT_ELBOW),
                                     G.point(kpts, G.RIGHT_WRIST))
                       if has_right else None)

        if self._left.update(angle_left, now_sec):
            s.reps_left += 1
        if self._right.update(angle_right, now_sec):
            s.reps_right += 1

        s.tracking = self._left.has_reading() or self._right.has_reading()
        # Primary angle = whichever arm is further into the curl.
        if self._left.has_reading() and self._right.has_reading():
            s.angle = min(self._left.smoothed, self._right.smoothed)
        elif self._left.has_reading():
            s.angle = self._left.smoothed
        elif self._right.has_reading():
            s.angle = self._right.smoothed
        s.has_angle = s.tracking

        curling = self._left.flexed() or self._right.flexed()
        s.stage = "curl" if curling else ("extend" if s.tracking else "idle")

        self._check_elbow_drift(kpts, has_left, has_right)

        paired_now = self._paired_reps()
        gained = paired_now - self._paired
        self._paired = paired_now
        return max(0, gained)

    def _check_elbow_drift(self, kpts, has_left, has_right) -> None:
        s = self.state
        t = self.kpt_thres
        if has_left and G.visible(kpts, G.LEFT_HIP, t):
            drift = G.joint_angle(G.point(kpts, G.LEFT_ELBOW),
                                  G.point(kpts, G.LEFT_SHOULDER),
                                  G.point(kpts, G.LEFT_HIP))
            if drift is not None and drift > _CURL_ELBOW_DRIFT:
                s.form_warning = "Left elbow drifting - keep it at your side"
                return
        if has_right and G.visible(kpts, G.RIGHT_HIP, t):
            drift = G.joint_angle(G.point(kpts, G.RIGHT_ELBOW),
                                  G.point(kpts, G.RIGHT_SHOULDER),
                                  G.point(kpts, G.RIGHT_HIP))
            if drift is not None and drift > _CURL_ELBOW_DRIFT:
                s.form_warning = "Right elbow drifting - keep it at your side"

    def _paired_reps(self) -> int:
        seen_left = self._left.ever_read()
        seen_right = self._right.ever_read()
        if seen_left and seen_right:
            return min(self.state.reps_left, self.state.reps_right)
        return max(self.state.reps_left, self.state.reps_right)


# --------------------------------------------------------------------------- #
# Registry.
# --------------------------------------------------------------------------- #
_REGISTRY = {c.id: c for c in (Squat, PushUp, HammerCurl)}


def create_exercise(mode: str, kpt_thres: float = 0.5) -> Optional[Exercise]:
    cls = _REGISTRY.get(mode)
    return cls(kpt_thres) if cls else None


def exercise_ids() -> List[str]:
    return list(_REGISTRY.keys())
