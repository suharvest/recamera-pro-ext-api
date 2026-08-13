"""Offline tests for MQTT state aggregation (no broker required)."""
from __future__ import annotations

import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.mqtt_sink import MqttSink  # noqa: E402


def test_build_state_reports_true_person_and_fallen_counts():
    results = [
        {"kind": "person", "track_id": 1, "state": "normal",
         "fall_detected": False, "score": 0.9},
        {"kind": "person", "track_id": 2, "state": "fallen",
         "fall_detected": True, "score": 0.8},
        {"kind": "car", "cls_name": "car", "score": 0.7},
    ]
    events = [
        # Put the edge event first to prove a later normal pose_state cannot
        # erase its event id in the compatibility summary.
        {"kind": "fall", "track_id": 2, "event_id": 4},
        {"kind": "pose_state", "track_id": 1, "state": "normal",
         "fall_detected": False},
        {"kind": "pose_state", "track_id": 2, "state": "fallen",
         "fall_detected": True},
    ]

    state = MqttSink._build_state("fall-detection", 7, 1.25, results, events)

    # Existing fields remain intact while the new counters reflect results,
    # rather than the number/kind of app events.
    assert state["results_count"] == 3
    assert state["person_count"] == 2
    assert state["fallen_count"] == 1
    assert state["summary"]["person_count"] == 2
    assert state["summary"]["fallen_count"] == 1
    assert state["summary"]["fall_detected"] is True
    assert state["summary"]["event_id"] == 4
    assert state["counts_by_kind"] == {"pose_state": 2, "fall": 1}


def test_build_state_keeps_legacy_person_fallbacks():
    # Older/other pose consumers may only provide cls_name or track/state.
    results = [
        {"cls_name": "person", "fall_detected": True},
        {"track_id": 9, "state": "normal", "fall_detected": False},
    ]
    state = MqttSink._build_state("legacy", 1, 0.0, results, [])
    assert state["person_count"] == 2
    assert state["fallen_count"] == 1


def test_fall_contract_matches_cross_platform_shape():
    sink = object.__new__(MqttSink)
    sink._seq = 9
    sink._fall_global_event_id = 2
    sink.app_id = "fall-detection"
    sink._frame_w = 1280
    sink._frame_h = 720
    payload = {
        "stream_id": "camera-0",
        "inference_time_ms": 12.5,
        "pipeline_ms": 18.0,
        "results": [{
            "kind": "person", "track_id": 3, "box": [128, 72, 640, 648],
            "score": 0.9, "keypoints": [[320, 180, 0.8]] * 17,
            "features": {"valid": True, "hip_drop_speed": 0.1,
                         "hip_drop_distance": 0.0, "torso_angle_deg": 10.0,
                         "bbox_aspect_ratio": 0.9},
        }],
        "events": [
            {"kind": "pose_state", "track_id": 3, "visible": True,
             "state": "fallen", "fall_detected": True, "event_id": 1,
             "missed_frames": 0},
            {"kind": "fall", "track_id": 3, "event_id": 1},
        ],
    }
    state = sink._fall_contract(payload, 1.0)
    required = {"timestamp", "frame_id", "inference_time_ms", "stream_id",
                "fall_detected", "fall_event", "event_id", "global_event_id",
                "event_id_scope", "state", "person_detected", "person_count",
                "fallen_count", "tracking", "features", "keypoints", "pose17",
                "persons"}
    assert required <= state.keys()
    assert state["event_id"] == state["global_event_id"] == 3
    assert state["event_id_scope"] == "stream_global_event_id"
    assert state["person_count"] == state["fallen_count"] == 1
    assert len(state["persons"][0]["pose17"]) == 17
