"""Offline tests for the fall app's identity + per-track state plumbing."""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def _person(box):
    return {"box": list(box), "score": 0.9}


def _load_app_module():
    path = os.path.join(_HERE, "app.py")
    spec = importlib.util.spec_from_file_location("fall_detection_test_app", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _pose(box):
    """Make a valid COCO-17 pose with an upright torso."""
    x1, y1, x2, y2 = box
    cx = (x1 + x2) * 0.5
    shoulder_y = y1 + (y2 - y1) * 0.28
    hip_y = y1 + (y2 - y1) * 0.58
    kpts = [[cx, shoulder_y, 0.9] for _ in range(17)]
    kpts[5] = [cx - 8, shoulder_y, 0.9]
    kpts[6] = [cx + 8, shoulder_y, 0.9]
    kpts[11] = [cx - 7, hip_y, 0.9]
    kpts[12] = [cx + 7, hip_y, 0.9]
    return {"box": list(box), "score": 0.9, "keypoints": kpts}


def test_iou_tracker_keeps_ids_through_reordering_and_short_occlusion():
    mod = _load_app_module()
    IoUTracker = mod.IoUTracker
    tracker = IoUTracker(iou_threshold=0.2, max_lost_sec=0.75)
    first = [_person([0, 0, 40, 80]), _person([100, 0, 140, 80])]
    tracks = tracker.update(first, 0.0)
    assert [t.track_id for t in tracks] == [1, 2]

    # The model may return detections in a different score/order sequence.
    swapped = [_person([100, 0, 140, 80]), _person([0, 0, 40, 80])]
    tracks = tracker.update(swapped, 0.1)
    # Use tracker assignments (rather than output order) to annotate the
    # detection and verify each box retained its original identity.
    for tr in tracks:
        assert tr.detection_index is not None
        swapped[tr.detection_index]["track_id"] = tr.track_id
    assert {round(r["box"][0]): r["track_id"] for r in swapped} == {
        100: 2, 0: 1}

    # One empty frame is retained as a lost track, not a new identity.
    lost = tracker.update([], 0.2)
    assert {t.track_id for t in lost} == {1, 2}
    assert all(not t.visible for t in lost)
    assert tracker.expired_ids == []

    returned = [_person([0, 0, 40, 80])]
    tracks = tracker.update(returned, 0.3)
    visible = [t for t in tracks if t.visible]
    assert len(visible) == 1 and visible[0].track_id == 1
    assert visible[0].detection_index == 0
    assert tracker.track_count == 1

    # A track that is missing beyond the configured grace is removed and can
    # never be silently revived by a coincident later box.
    tracker.update([], 1.2)
    assert set(tracker.expired_ids) == {1, 2}
    assert tracker.active_tracks() == []


def test_fall_app_runs_one_detector_per_track_and_feeds_invalid_on_loss():
    mod = _load_app_module()
    app = mod.FallDetectionApp()
    app.setup({"keypoint_confidence": 0.5, "occlusion_grace_sec": 0.5})
    frame = SimpleNamespace(pts=0.0, w=200, h=100)

    results = [_pose([0, 0, 50, 90]), _pose([100, 0, 150, 90])]
    events = app.on_results(results, frame)
    assert {r["track_id"] for r in results} == {1, 2}
    assert len(app.detectors) == 2
    assert all(r["state"] == "normal" for r in results)
    assert {e["track_id"] for e in events if e["kind"] == "pose_state"} == {1, 2}

    # Brief occlusion advances both independent state machines with invalid
    # observations and retains their identities.
    frame.pts = 0.2
    events = app.on_results([], frame)
    states = [e for e in events if e["kind"] == "pose_state"]
    assert {e["track_id"] for e in states} == {1, 2}
    assert all(e["visible"] is False for e in states)
    assert len(app.detectors) == 2

    # Returning within the grace revives the same id; later timeout removes
    # both detector instances.
    frame.pts = 0.3
    returned = [_pose([100, 0, 150, 90])]
    app.on_results(returned, frame)
    assert returned[0]["track_id"] == 2
    assert returned[0]["state"] == "normal"
    assert returned[0]["fall_detected"] is False
    frame.pts = 1.0
    app.on_results([], frame)
    assert app.detectors == {}
