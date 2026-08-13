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
import gzip
import math
import os
import sys
from collections import deque
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence

import numpy as np

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


class _TemporalProfile:
    """Frozen NumPy weights for the production 48-frame pose gate."""

    window = 48
    frame_dim = 56
    feature_dim = 504
    sample_fps = 15.0
    stride = 3

    def __init__(self, path: str) -> None:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            data = json.load(source)
        self.frame_mask = np.asarray(data["frame_mask"], dtype=np.float32)
        self.mean = np.asarray(data["mean"], dtype=np.float32)
        self.scale = np.asarray(data["scale"], dtype=np.float32)
        self.w1 = np.asarray(data["w1"], dtype=np.float32).reshape(
            self.feature_dim, -1)
        self.b1 = np.asarray(data["b1"], dtype=np.float32)
        self.w2 = np.asarray(data["w2"], dtype=np.float32)
        self.b2 = float(data["b2"])
        self.threshold = float(data["threshold"])
        self.consecutive = int(data["consecutive"])
        if (self.frame_mask.shape != (self.frame_dim,) or
                self.mean.shape != (self.feature_dim,) or
                self.scale.shape != (self.feature_dim,) or
                self.w1.shape[0] != self.feature_dim or
                self.w1.shape[1:] != self.b1.shape or
                self.w2.shape != self.b1.shape):
            raise ValueError(f"invalid temporal profile shapes in {path}")


class _TemporalClassifier:
    """Per-track learned gate; a few NumPy matrix ops every third frame."""

    def __init__(self, profile: _TemporalProfile) -> None:
        self.profile = profile
        self.frames = deque()
        self.last_timestamp = -1.0
        self.last_evaluation = -1.0
        self.positive_run = 0
        self.last_probability = 0.0
        self.last_positive = False

    def reset(self) -> None:
        self.frames.clear()
        self.last_timestamp = -1.0
        self.last_evaluation = -1.0
        self.positive_run = 0
        self.last_probability = 0.0
        self.last_positive = False

    def update(self, frame: np.ndarray, timestamp: float):
        p = self.profile
        timestamp = float(timestamp)
        if self.last_timestamp >= 0.0 and timestamp < self.last_timestamp:
            self.reset()
        self.last_timestamp = timestamp
        self.frames.append((timestamp, frame * p.frame_mask))
        cutoff = timestamp - (p.window - 1) / p.sample_fps - 0.5
        while len(self.frames) > 1 and self.frames[1][0] < cutoff:
            self.frames.popleft()

        evaluated = (self.last_evaluation < 0.0 or
                     timestamp - self.last_evaluation >=
                     p.stride / p.sample_fps - 1e-6)
        if evaluated:
            self.last_evaluation = timestamp
            self.last_probability = self._evaluate(timestamp)
            if self.last_probability >= p.threshold:
                self.positive_run += 1
            else:
                self.positive_run = 0
            self.last_positive = self.positive_run >= p.consecutive
        return evaluated, self.last_positive, self.last_probability

    def _evaluate(self, timestamp: float) -> float:
        p = self.profile
        history = list(self.frames)
        sequence = np.empty((p.window, p.frame_dim), dtype=np.float32)
        source = 0
        for i in range(p.window):
            sample_time = timestamp - (p.window - 1 - i) / p.sample_fps
            while (source + 1 < len(history) and
                   history[source + 1][0] <= sample_time + 1e-6):
                source += 1
            sequence[i] = history[source][1]

        bins = sequence.reshape(6, p.window // 6, p.frame_dim).mean(axis=1)
        feature = np.concatenate((
            bins.reshape(-1),
            sequence.std(axis=0),
            sequence[-1] - sequence[0],
            sequence.max(axis=0) - sequence.min(axis=0),
        )).astype(np.float32, copy=False)
        normalized = (feature - p.mean) / np.maximum(p.scale, 1e-12)
        hidden = np.maximum(0.0, normalized @ p.w1 + p.b1)
        logit = float(hidden @ p.w2 + p.b2)
        if logit >= 0.0:
            return 1.0 / (1.0 + math.exp(-logit))
        exp_logit = math.exp(logit)
        return exp_logit / (1.0 + exp_logit)


def _make_temporal_frame(person: Optional[dict], obs, frame_w: int,
                         frame_h: int) -> np.ndarray:
    """Exact 56-value pelvis-centred COCO-17 representation used on Jetson."""
    out = np.zeros(56, dtype=np.float32)
    if person is None or frame_w <= 0 or frame_h <= 0:
        return out
    kpts = person.get("keypoints")
    if not isinstance(kpts, (list, tuple)) or len(kpts) < N_KPT:
        return out
    try:
        pose = np.asarray(kpts[:N_KPT], dtype=np.float32)
    except (TypeError, ValueError):
        return out
    if pose.shape != (N_KPT, 3) or not np.all(np.isfinite(pose)):
        return out
    xy = pose[:, :2] / np.asarray([frame_w, frame_h], dtype=np.float32)
    confidence = np.clip(pose[:, 2], 0.0, 1.0)

    def midpoint(a: int, b: int):
        weight = float(confidence[a] + confidence[b])
        if weight < 0.1:
            return np.zeros(2, dtype=np.float32), 0.0
        point = (xy[a] * confidence[a] + xy[b] * confidence[b]) / weight
        return point, weight * 0.5

    hip, hip_conf = midpoint(11, 12)
    shoulder, shoulder_conf = midpoint(5, 6)
    if hip_conf < 0.1:
        visible = confidence >= 0.1
        weight = float(confidence[visible].sum())
        if weight <= 0.0:
            return out
        hip = (xy[visible] * confidence[visible, None]).sum(axis=0) / weight

    scale = float(np.linalg.norm(shoulder - hip)) if shoulder_conf >= 0.1 else 0.0
    if scale < 0.04:
        visible = confidence >= 0.1
        if np.any(visible):
            span = xy[visible].max(axis=0) - xy[visible].min(axis=0)
            scale = float(span.max()) * 0.35
    scale = max(scale, 0.04)

    visible = confidence >= 0.1
    centred = np.clip((xy - hip) / scale, -4.0, 4.0)
    out[:34].reshape(N_KPT, 2)[visible] = centred[visible]
    out[34:51] = confidence
    out[51] = float(obs.hip_y)
    out[52] = float(obs.torso_angle_deg) / 90.0
    out[53] = min(float(obs.bbox_aspect_ratio), 4.0) / 4.0
    out[54] = float(obs.person_score)
    out[55] = 1.0 if obs.valid else 0.0
    return out


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
    # Only skeleton coordinates are consumed -- never frame.data pixels -- so the
    # frame source can letterbox on RGA into data itself (see App.model_frame).
    model_frame = "hw-direct"

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
            temporal_confirmation_required=bool(
                params.get("temporal_confirmation_required", True)),
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
        profile_file = str(params.get(
            "temporal_profile_file", "models/temporal_yolo11s_pose_v1.json.gz"))
        if not os.path.isabs(profile_file):
            profile_file = os.path.join(_here, profile_file)
        self._temporal_profile = _TemporalProfile(profile_file)
        self.temporal_classifiers = {}
        self.tracker = IoUTracker(
            iou_threshold=float(params.get("tracker_iou_threshold", 0.2)),
            max_lost_sec=cfg.occlusion_grace_sec,
        )
        self.detectors = {}
        print(f"[fall] setup conf={self.conf} kpt_thres={self.kpt_thres} "
              f"torso>={cfg.torso_angle_threshold_deg} aspect>="
              f"{cfg.bbox_aspect_ratio_threshold} confirm={cfg.confirmation_sec}s "
              f"track_iou>={self.tracker.iou_threshold} "
              f"track_timeout={self.tracker.max_lost_sec}s "
              f"temporal=strict:{cfg.temporal_confirmation_required} "
              f"window={self._temporal_profile.window}",
              flush=True)

    def on_config_reload(self, config):
        """★S1 live hot-reload★ (SIGHUP -> re-read config.json).

        fall-detection's live knobs are the two thresholds (self.conf /
        self.kpt_thres) plus the full FallConfig, which is copied per-track into
        each FallDetector. Reapply by VALUE-REPLACE: rebuild the shared
        FallConfig template (so NEW tracks inherit it) and push a fresh copy into
        every EXISTING detector via set_config() -- which swaps the config
        without touching the per-track temporal state machine (no reset()). The
        tracker's occlusion timeout tracks occlusion_grace_sec. Nothing here
        reloads the pose model or clears track history.
        """
        params = {k: v for k, v in (config or {}).items() if v is not None}
        self.config = config or {}

        def _f(key, cur):
            try:
                return float(params.get(key, cur))
            except (TypeError, ValueError):
                return cur

        self.conf = _f("confidence", self.conf)
        self.kpt_thres = _f("keypoint_confidence", self.kpt_thres)

        cur = self._fall_config
        cfg = FallConfig(
            temporal_confirmation_required=bool(params.get(
                "temporal_confirmation_required",
                cur.temporal_confirmation_required)),
            hip_drop_speed_threshold=_f("hip_drop_speed_threshold",
                                        cur.hip_drop_speed_threshold),
            hip_drop_distance_threshold=_f("hip_drop_distance_threshold",
                                           cur.hip_drop_distance_threshold),
            motion_window_sec=_f("motion_window_sec", cur.motion_window_sec),
            torso_angle_threshold_deg=_f("torso_angle_threshold_deg",
                                         cur.torso_angle_threshold_deg),
            bbox_aspect_ratio_threshold=_f("bbox_aspect_ratio_threshold",
                                           cur.bbox_aspect_ratio_threshold),
            min_suspected_features=int(_f("min_suspected_features",
                                          cur.min_suspected_features)),
            confirmation_sec=_f("confirmation_sec", cur.confirmation_sec),
            suspected_timeout_sec=_f("suspected_timeout_sec", cur.suspected_timeout_sec),
            occlusion_grace_sec=_f("occlusion_grace_sec", cur.occlusion_grace_sec),
            recovery_torso_angle_deg=_f("recovery_torso_angle_deg",
                                        cur.recovery_torso_angle_deg),
            recovery_aspect_ratio=_f("recovery_aspect_ratio", cur.recovery_aspect_ratio),
            recovery_window_sec=_f("recovery_window_sec", cur.recovery_window_sec),
            cooldown_sec=_f("cooldown_sec", cur.cooldown_sec),
        )
        self._fall_config = cfg
        # Swap config into live per-track detectors WITHOUT resetting their state.
        for detector in self.detectors.values():
            detector.set_config(replace(cfg))
        # occlusion grace also bounds tracker retention (see setup()).
        if getattr(self, "tracker", None) is not None:
            self.tracker.max_lost_sec = cfg.occlusion_grace_sec
        print(f"[fall] hot-reload conf={self.conf} kpt_thres={self.kpt_thres} "
              f"torso>={cfg.torso_angle_threshold_deg} "
              f"aspect>={cfg.bbox_aspect_ratio_threshold} "
              f"confirm={cfg.confirmation_sec}s cooldown={cfg.cooldown_sec}s "
              f"(tracks={len(self.detectors)})", flush=True)

    def _detector_for(self, track_id):
        detector = self.detectors.get(track_id)
        if detector is None:
            # FallDetector currently clamps its config in-place.  Give every
            # track a dataclass copy so one track can never mutate another's
            # thresholds or temporal state.
            detector = FallDetector(replace(self._fall_config))
            self.detectors[track_id] = detector
        return detector

    def _temporal_for(self, track_id):
        classifier = self.temporal_classifiers.get(track_id)
        if classifier is None:
            classifier = _TemporalClassifier(self._temporal_profile)
            self.temporal_classifiers[track_id] = classifier
        return classifier

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
            temporal = self._temporal_for(track.track_id)
            temporal_frame = _make_temporal_frame(
                person, obs, int(frame.w), int(frame.h))
            evaluated, temporal_positive, temporal_probability = temporal.update(
                temporal_frame, frame.pts)
            out = detector.update(
                obs,
                # The frozen profile is available on every frame. ``positive``
                # deliberately persists between stride-3 evaluations, exactly
                # matching the Jetson tracker adapter.
                temporal_available=True,
                temporal_positive=temporal_positive,
                temporal_probability=temporal_probability,
            )

            # Always surface the current state, including during a short
            # occlusion.  ``visible`` lets consumers distinguish a retained
            # state from a pose result in this frame.
            events.append({
                "kind": "pose_state",
                "track_id": track.track_id,
                "visible": person is not None,
                "missed_frames": track.missing_frames,
                "box": list(track.box),
                "state": out.state,
                "fall_detected": out.fall_detected,
                "event_id": out.event_id,
                "person_score": round(obs.person_score, 3),
                "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 1),
                "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 3),
                "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 3),
                "evidence_features": int(out.diagnostics.get("evidence_features", 0)),
                "features": {
                    "valid": bool(obs.valid),
                    "hip_y": round(obs.hip_y, 4),
                    "person_score": round(obs.person_score, 4),
                    "hip_drop_speed": round(out.diagnostics.get("hip_drop_speed", 0.0), 4),
                    "hip_drop_distance": round(out.diagnostics.get("hip_drop_distance", 0.0), 4),
                    "torso_angle_deg": round(out.diagnostics.get("torso_angle_deg", 0.0), 2),
                    "bbox_aspect_ratio": round(out.diagnostics.get("bbox_aspect_ratio", 0.0), 4),
                    "temporal_evaluated": bool(evaluated),
                    "temporal_probability": round(out.diagnostics.get("temporal_probability", 0.0), 4),
                    "temporal_positive": bool(out.diagnostics.get("temporal_positive", False)),
                },
            })

            if person is not None:
                person["track_id"] = track.track_id
                person["state"] = out.state
                person["fall_detected"] = out.fall_detected
                person["event_id"] = out.event_id
                person["person_detected"] = True
                person["person_score"] = float(person.get("score", 0.0))
                person["tracking"] = True
                person["missed_frames"] = 0
                person["features"] = events[-1]["features"]

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
            self.temporal_classifiers.pop(track_id, None)

        return events


if __name__ == "__main__":
    run_app(FallDetectionApp())
