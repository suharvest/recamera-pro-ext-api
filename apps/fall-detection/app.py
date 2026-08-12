#!/usr/bin/env python3
"""
fall-detection -- reCamera Pro pose app (port of the first-gen SSCMA solution).

Pipeline (all shared parts live in kit.app.App):
  live frame -> letterbox 640 -> YOLO11n-pose RKNN -> pose post-process
  -> on_results(): associate every pose with a lightweight IoU track, build a
     geometric Observation per person (hip_y / torso angle / box aspect), and
     advance one ported temporal FallDetector per track -> emit keypoints,
     per-person pose_state, and fall events.

Only the pose post-processor selection and the on_results() business logic are
app-specific; everything else is the generic Kit loop.

The 12 tuning parameters come from THIS app's manifest.json config_schema
(grouped detection/timing). appmgr's minimal launcher only forwards --model /
--sink / --port, so the app reads its own manifest for the fall thresholds --
this keeps appmgr generic and matches the first-gen 12-param schema exactly.

Run on device (inference requires root):
    KIT=/userdata/local/kit
    PYTHONPATH=$KIT python3 app.py \
        --model models/yolo11n_pose_rawhead_int8.rknn --sink ws --port 8124
"""
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    # The app is launched as a standalone ``app.py`` (the directory name has
    # a hyphen, so it is not an importable Python package).  Keep the tracker
    # local to this app without making it a shared-kit dependency.
    sys.path.insert(0, _here)
_kit_parent_env = os.environ.get("KIT_PARENT")
_kit_dir_env = os.environ.get("KIT_DIR")
for _cand in (
    _kit_parent_env,
    os.path.dirname(_kit_dir_env) if _kit_dir_env else None,
    "/userdata/local",                               # device: kit at /userdata/local/kit
    os.path.join(_here, ".."),                       # device: /userdata/local/apps
    os.path.join(_here, "..", ".."),                 # repo: recamera_pro/
    "/userdata/local/apps",
):
    if _cand and os.path.isdir(os.path.join(_cand, "kit")):
        _cand = os.path.abspath(_cand)
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from kit.app import App, run_app                                  # noqa: E402
from kit.runtime.postprocess import pose as pose_post             # noqa: E402
from kit.logic.geometry import make_observation, N_KPT            # noqa: E402
from kit.logic.temporal import FallDetector, FallConfig, FALLEN, RECOVERING  # noqa: E402


@dataclass
class _TrackedPerson:
    """One identity maintained by the app-local IoU tracker."""

    track_id: int
    box: List[float]
    last_seen: float
    missing_since: Optional[float] = None
    missing_frames: int = 0
    detection_index: Optional[int] = None

    @property
    def visible(self) -> bool:
        return self.detection_index is not None


class IoUTracker:
    """Greedy, deterministic IoU tracker with a short lost-track grace.

    This deliberately remains inside ``app.py`` because the app package
    builder ships ``app.py`` and ``models/`` only.  Keeping the tracker here
    makes the deployed fall app self-contained and does not alter shared kit
    tracker semantics used by other applications.
    """

    def __init__(self, iou_threshold: float = 0.2,
                 max_lost_sec: float = 0.75) -> None:
        self.iou_threshold = min(0.95, max(0.0, float(iou_threshold)))
        self.max_lost_sec = max(0.0, float(max_lost_sec))
        self._tracks: Dict[int, _TrackedPerson] = {}
        self._next_id = 1
        self.expired_ids: List[int] = []

    @staticmethod
    def _iou(a: Sequence[float], b: Sequence[float]) -> float:
        if len(a) < 4 or len(b) < 4:
            return 0.0
        ax1, ay1, ax2, ay2 = (float(v) for v in a[:4])
        bx1, by1, bx2, by2 = (float(v) for v in b[:4])
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0.0 else 0.0

    @staticmethod
    def _box(result: dict) -> Optional[List[float]]:
        box = result.get("box") if isinstance(result, dict) else None
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            return None
        try:
            out = [float(v) for v in box[:4]]
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in out):
            return None
        if out[2] <= out[0] or out[3] <= out[1]:
            return None
        return out

    def _expired(self, tr: _TrackedPerson, timestamp: float) -> bool:
        age = max(0.0, timestamp - tr.last_seen)
        if age > self.max_lost_sec:
            return True
        # With a zero grace, a repeated-PTS stream still gets one missing
        # update to feed invalid observation, then expires on the next one.
        return (self.max_lost_sec <= 0.0 and
                (tr.missing_frames > 0 or age > 0.0))

    def update(self, results: Sequence[dict], timestamp_sec: float
               ) -> List[_TrackedPerson]:
        """Associate pose results and return visible + non-expired tracks.

        A visible track carries ``detection_index`` into ``results``.  A lost
        track carries ``None`` so the caller can feed an invalid observation
        to its temporal detector.  IDs that pass the grace period are removed
        and listed in ``expired_ids``.
        """
        try:
            now = float(timestamp_sec)
        except (TypeError, ValueError):
            now = 0.0
        if not math.isfinite(now):
            now = 0.0

        self.expired_ids = []
        for tid, tr in list(self._tracks.items()):
            if self._expired(tr, now):
                self.expired_ids.append(tid)
                del self._tracks[tid]

        boxes: List[Optional[List[float]]] = [self._box(r) for r in results]
        pairs = []
        for tid, tr in self._tracks.items():
            for di, box in enumerate(boxes):
                if box is None:
                    continue
                score = self._iou(tr.box, box)
                if score >= self.iou_threshold:
                    pairs.append((score, tid, di))
        # Global best-overlap matching with explicit tie-breakers keeps IDs
        # stable when the model changes score/output order.
        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        matched_tracks = set()
        matched_dets = set()
        for _score, tid, di in pairs:
            if tid in matched_tracks or di in matched_dets:
                continue
            tr = self._tracks[tid]
            tr.box = list(boxes[di])  # type: ignore[arg-type]
            tr.last_seen = now
            tr.missing_since = None
            tr.missing_frames = 0
            tr.detection_index = di
            matched_tracks.add(tid)
            matched_dets.add(di)

        for tid, tr in self._tracks.items():
            if tid in matched_tracks:
                continue
            tr.detection_index = None
            tr.missing_frames += 1
            if tr.missing_since is None:
                tr.missing_since = now

        for di, box in enumerate(boxes):
            if di in matched_dets or box is None:
                continue
            tid = self._next_id
            self._next_id += 1
            self._tracks[tid] = _TrackedPerson(
                track_id=tid, box=list(box), last_seen=now,
                detection_index=di,
            )

        return [self._tracks[tid] for tid in sorted(self._tracks)]

    def active_tracks(self) -> List[_TrackedPerson]:
        return [self._tracks[tid] for tid in sorted(self._tracks)]

    @property
    def track_count(self) -> int:
        return sum(1 for tr in self._tracks.values() if tr.visible)


class FallDetectionApp(App):
    id = "fall-detection"
    name = "Fall Detection"
    postproc = "pose"

    def setup(self, config):
        super().setup(config)
        # `config` is the effective config assembled by kit.config in run_app:
        # manifest config_schema defaults overlaid by <app_dir>/config.json.
        # The 12 fall-tuning params are read straight from it (single entry).
        params = {k: v for k, v in (config or {}).items() if v is not None}

        # Person / keypoint confidence for the pose post-process.
        self.conf = float(params.get("confidence", 0.4))
        self.kpt_thres = float(params.get("keypoint_confidence", 0.5))

        cfg = FallConfig(
            hip_drop_speed_threshold=float(params.get("hip_drop_speed_threshold", 0.25)),
            hip_drop_distance_threshold=float(params.get("hip_drop_distance_threshold", 0.02)),
            motion_window_sec=float(params.get("motion_window_sec", 0.75)),
            torso_angle_threshold_deg=float(params.get("torso_angle_threshold_deg", 55.0)),
            bbox_aspect_ratio_threshold=float(params.get("bbox_aspect_ratio_threshold", 1.25)),
            min_suspected_features=int(params.get("min_suspected_features", 2)),
            confirmation_sec=float(params.get("confirmation_sec", 0.80)),
            suspected_timeout_sec=float(params.get("suspected_timeout_sec", 1.50)),
            occlusion_grace_sec=float(params.get("occlusion_grace_sec", 0.75)),
            recovery_torso_angle_deg=float(params.get("recovery_torso_angle_deg", 35.0)),
            recovery_aspect_ratio=float(params.get("recovery_aspect_ratio", 1.10)),
            recovery_window_sec=float(params.get("recovery_window_sec", 2.00)),
            cooldown_sec=float(params.get("cooldown_sec", 3.00)),
        )
        # Pose detections have no identity.  Associate boxes first, then keep
        # one independent temporal state machine per track.  The same
        # occlusion grace used by the detector also bounds tracker retention;
        # a missing track is fed an invalid Observation until this timeout.
        self._fall_config = cfg
        self.tracker = IoUTracker(
            iou_threshold=float(params.get("tracker_iou_threshold", 0.2)),
            max_lost_sec=cfg.occlusion_grace_sec,
        )
        self.detectors = {}
        print(f"[fall] setup conf={self.conf} kpt_thres={self.kpt_thres} "
              f"torso>={cfg.torso_angle_threshold_deg} aspect>="
              f"{cfg.bbox_aspect_ratio_threshold} confirm={cfg.confirmation_sec}s "
              f"track_iou>={self.tracker.iou_threshold} "
              f"track_timeout={self.tracker.max_lost_sec}s",
              flush=True)

    def _detector_for(self, track_id):
        detector = self.detectors.get(track_id)
        if detector is None:
            # FallDetector currently clamps its config in-place.  Give every
            # track a dataclass copy so one track can never mutate another's
            # thresholds or temporal state.
            detector = FallDetector(replace(self._fall_config))
            self.detectors[track_id] = detector
        return detector

    def run_postproc(self, outs, info):
        return pose_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou,
                                     kpt_thres=self.kpt_thres)

    def on_results(self, results, frame):
        results = list(results or [])
        events = []
        # Pose post-processing normally guarantees a valid box, but keep the
        # app's historical ``kind=person`` output marker even for a malformed
        # result so downstream counters remain backward-compatible.
        for result in results:
            if isinstance(result, dict):
                result.setdefault("kind", "person")

        # The tracker returns visible and briefly-lost identities.  Every
        # track advances its own detector; a lost one receives ``None`` so the
        # temporal state machine can apply its post-impact occlusion grace.
        tracks = self.tracker.update(results, frame.pts)
        for track in tracks:
            person = None
            if track.detection_index is not None:
                idx = track.detection_index
                if 0 <= idx < len(results):
                    person = results[idx]

            detector = self._detector_for(track.track_id)
            obs = make_observation(person, frame.pts, frame.h, self.kpt_thres)
            out = detector.update(obs)

            # Always surface the current state, including during a short
            # occlusion.  ``visible`` lets consumers distinguish a retained
            # state from a pose result in this frame.
            events.append({
                "kind": "pose_state",
                "track_id": track.track_id,
                "visible": person is not None,
                "state": out.state,
                "fall_detected": out.fall_detected,
                "event_id": out.event_id,
                "person_score": round(obs.person_score, 3),
                "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 1),
                "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 3),
                "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 3),
                "evidence_features": int(out.diagnostics.get("evidence_features", 0)),
            })

            if person is not None:
                person["track_id"] = track.track_id
                person["state"] = out.state
                person["fall_detected"] = out.fall_detected
                person["event_id"] = out.event_id

            # Edge event: a fall was just confirmed for THIS identity.
            if out.fall_event:
                events.append({
                    "kind": "fall",
                    "track_id": track.track_id,
                    "event_id": out.event_id,
                    "state": out.state,
                })
                print(f"[fall] *** FALL event track={track.track_id} "
                      f"#{out.event_id} at pts={frame.pts:.2f} ***", flush=True)

        # Once a track has exceeded the grace, discard its temporal state so a
        # later appearance gets a fresh id and cannot inherit an old alarm.
        for track_id in self.tracker.expired_ids:
            self.detectors.pop(track_id, None)

        return events


if __name__ == "__main__":
    run_app(FallDetectionApp())
