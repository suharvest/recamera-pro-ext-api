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
