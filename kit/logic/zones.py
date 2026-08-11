"""
Spatial & temporal counting on top of `kit.logic.tracker` tracks.

Ported from the first-gen retail-vision C++ (`zone_metrics.cpp` +
the dwell / entry-line parts of `person_tracker.cpp`). All are model-free and
operate on `Track` objects (or any object exposing `.foot`, `.prev_foot`,
`.track_id`, `.speed_px_s`) so any "detect + track + count" app reuses them.

  ZoneCounter  -- occupancy inside a normalised polygon (foot-point test).
  LineCounter  -- directed entry/exit counting across a normalised segment
                  (cross-product sign decides in vs out), one count per crossing.
  Dwell        -- per-track stationary state machine (browsing / engaged /
                  assistance) with the C++ decay-tolerant stationary counter.
  RollingWindow-- occupancy median-smoothing + peak / averages over a time window.

Coordinates are normalised to [0,1]; dwell speed thresholds are px/s in the
nominal 640 frame (matching `Track.speed_px_s`), so first-gen defaults carry over.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from kit.logic.geometry import point_in_polygon, segment_crossing

# Dwell states (retail semantics). BROWSING = transient/dwelling, ENGAGED =
# stationary past the engaged threshold (驻足), ASSISTANCE = stationary long
# enough to likely need help (求助).
BROWSING = "browsing"
ENGAGED = "engaged"
ASSISTANCE = "assistance"


# --------------------------------------------------------------------------- #
# Zone occupancy
# --------------------------------------------------------------------------- #
class ZoneCounter:
    """Counts tracks whose FOOT point lies inside a normalised polygon.

    An empty / <3-point polygon means "whole frame" (every track counts), which
    is the default so an unconfigured app still reports total occupancy."""

    def __init__(self, polygon: Optional[Sequence[Sequence[float]]] = None) -> None:
        self.polygon: List[Tuple[float, float]] = []
        self.set_polygon(polygon)

    def set_polygon(self, polygon: Optional[Sequence[Sequence[float]]]) -> None:
        if polygon and len(polygon) >= 3:
            self.polygon = [(float(p[0]), float(p[1])) for p in polygon]
        else:
            self.polygon = []

    @property
    def enabled(self) -> bool:
        return len(self.polygon) >= 3

    def contains(self, foot: Tuple[float, float]) -> bool:
        if not self.enabled:
            return True
        return point_in_polygon(foot[0], foot[1], self.polygon)

    def inside(self, tracks: Sequence) -> List:
        return [tr for tr in tracks if self.contains(tr.foot)]

    def count(self, tracks: Sequence) -> int:
        return len(self.inside(tracks))


# --------------------------------------------------------------------------- #
# Entry / exit line
# --------------------------------------------------------------------------- #
class LineCounter:
    """Directed entry/exit counting across a finite normalised segment a->b.

    Each frame, a track's previous foot -> current foot segment is tested for a
    genuine crossing of a->b (both segments must strictly straddle). A left->right
    crossing (cross-product sign +1) counts as an ENTRY when `ab_in` is True,
    else an EXIT; the sign flips for the opposite direction. Because it compares
    consecutive foot points, each physical crossing is counted exactly once."""

    def __init__(self, a: Optional[Sequence[float]] = None,
                 b: Optional[Sequence[float]] = None, ab_in: bool = True) -> None:
        self.a: Optional[Tuple[float, float]] = None
        self.b: Optional[Tuple[float, float]] = None
        self.ab_in = bool(ab_in)
        self.entry_count = 0
        self.exit_count = 0
        if a is not None and b is not None:
            self.set_line(a, b, ab_in)

    def set_line(self, a: Sequence[float], b: Sequence[float],
                 ab_in: bool = True) -> None:
        self.a = (float(a[0]), float(a[1]))
        self.b = (float(b[0]), float(b[1]))
        self.ab_in = bool(ab_in)

    @property
    def enabled(self) -> bool:
        return self.a is not None and self.b is not None

    def update(self, tracks: Sequence) -> List[dict]:
        """Test every track for a crossing this frame. Returns a list of
        {"track_id","dir"} events ("in"/"out") and updates the counters."""
        events: List[dict] = []
        if not self.enabled:
            return events
        ax, ay = self.a
        bx, by = self.b
        for tr in tracks:
            p0 = tr.prev_foot
            p1 = tr.foot
            if p0 == p1:
                continue
            d = segment_crossing(ax, ay, bx, by, p0[0], p0[1], p1[0], p1[1])
            if d == 0:
                continue
            is_in = (d > 0) == self.ab_in
            if is_in:
                self.entry_count += 1
            else:
                self.exit_count += 1
            events.append({"track_id": tr.track_id,
                           "dir": "in" if is_in else "out"})
        return events


# --------------------------------------------------------------------------- #
# Dwell state machine (per track)
# --------------------------------------------------------------------------- #
@dataclass
class DwellConfig:
    speed_threshold: float = 10.0        # px/s below which a track is stationary
    min_frames: int = 5                  # stationary frames to confirm dwell
    engaged_sec: float = 1.5             # dwell >= this -> ENGAGED (驻足)
    assistance_sec: float = 20.0         # dwell >= this -> ASSISTANCE (求助)
    stable_threshold: int = 30           # frames before "stable" (slow decay)
    decay_slow: int = 2                  # stationary-frame decay when stable
    decay_fast: int = 5                  # stationary-frame decay when not stable


@dataclass
class _DwellState:
    stationary_frames: int = 0
    state: str = BROWSING
    dwell_start: float = 0.0
    duration: float = 0.0


class Dwell:
    """Tracks per-track stationary dwell time and classifies browsing / engaged /
    assistance. Mirrors PersonTracker::updateDwellState + updateStationaryFrames
    (decay-tolerant so brief gestures don't reset a confirmed dwell)."""

    def __init__(self, config: Optional[DwellConfig] = None) -> None:
        self.cfg = config or DwellConfig()
        self._states: Dict[int, _DwellState] = {}

    def _update_stationary(self, st: _DwellState, is_stationary: bool) -> None:
        if is_stationary:
            st.stationary_frames += 1
        elif st.stationary_frames > self.cfg.stable_threshold:
            st.stationary_frames = max(0, st.stationary_frames - self.cfg.decay_slow)
        else:
            st.stationary_frames = max(0, st.stationary_frames - self.cfg.decay_fast)

    def update(self, track, t: float) -> str:
        """Advance one track's dwell state; returns its current state string."""
        st = self._states.get(track.track_id)
        if st is None:
            st = _DwellState()
            self._states[track.track_id] = st

        is_stationary = track.speed_px_s < self.cfg.speed_threshold
        self._update_stationary(st, is_stationary)
        is_still = is_stationary and st.stationary_frames >= self.cfg.min_frames

        if not is_still:
            if st.stationary_frames == 0:
                st.state = BROWSING
                st.dwell_start = 0.0
                st.duration = 0.0
            return st.state

        if st.dwell_start <= 0.0:
            st.dwell_start = t
        st.duration = t - st.dwell_start
        if st.duration >= self.cfg.assistance_sec:
            st.state = ASSISTANCE
        elif st.duration >= self.cfg.engaged_sec:
            st.state = ENGAGED
        else:
            st.state = BROWSING
        return st.state

    def duration(self, track_id: int) -> float:
        st = self._states.get(track_id)
        return st.duration if st else 0.0

    def prune(self, live_ids: Sequence[int]) -> None:
        """Forget state for tracks that no longer exist (call each frame)."""
        live = set(live_ids)
        for tid in [t for t in self._states if t not in live]:
            del self._states[tid]


# --------------------------------------------------------------------------- #
# Rolling-window occupancy statistics
# --------------------------------------------------------------------------- #
@dataclass
class StateCount:
    total: int = 0
    browsing: int = 0
    engaged: int = 0
    assistance: int = 0


@dataclass
class WindowSnapshot:
    occupancy: int = 0
    browsing: int = 0
    engaged: int = 0
    assistance: int = 0
    peak: int = 0
    entry_count: int = 0
    exit_count: int = 0


class RollingWindow:
    """Median-smooths occupancy and reports peak over a sliding time window.

    Port of ZoneMetrics: a 5-sample median filter suppresses single-frame
    occupancy jitter, occupancy is sampled once per second for the peak, and
    samples older than `window_sec` are pruned."""

    SMOOTH = 5

    def __init__(self, window_sec: float = 60.0) -> None:
        self.window_sec = float(window_sec)
        self._history: Deque[int] = deque(maxlen=self.SMOOTH)
        self._samples: Deque[Tuple[float, int]] = deque()
        self._last_sample_t: float = -1.0
        self._smoothed: int = 0
        self._counts = StateCount()
        self._entry = 0
        self._exit = 0

    def _smooth(self, raw: int) -> int:
        self._history.append(raw)
        s = sorted(self._history)
        return s[len(s) // 2]

    def update(self, counts: StateCount, entry_count: int, exit_count: int,
               t: float) -> None:
        self._counts = counts
        self._entry = entry_count
        self._exit = exit_count
        self._smoothed = self._smooth(counts.total)
        if self._last_sample_t < 0 or (t - self._last_sample_t) >= 1.0:
            self._samples.append((t, self._smoothed))
            self._last_sample_t = t
        cutoff = t - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def snapshot(self) -> WindowSnapshot:
        peak = 0
        for _, c in self._samples:
            if c > peak:
                peak = c
        return WindowSnapshot(
            occupancy=self._smoothed,
            browsing=self._counts.browsing,
            engaged=self._counts.engaged,
            assistance=self._counts.assistance,
            peak=peak,
            entry_count=self._entry,
            exit_count=self._exit,
        )
