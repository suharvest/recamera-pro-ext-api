#!/usr/bin/env python3
"""Extract 15 FPS GMDCSA traces with the reCamera Pro RV1126B RKNN model.

Run this script on a reCamera Pro.  Video decode stays in ffmpeg, pose
inference stays in RKNNLite/NPU, and Python only performs decode orchestration,
raw-head post-processing and deterministic single-subject trace selection.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np


FPS = 15.0


def load_app(app_dir: Path):
    path = app_dir / "app.py"
    spec = importlib.util.spec_from_file_location("recamera_pro_fall_trace", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe_size(path: Path) -> tuple[int, int]:
    if shutil.which("ffprobe"):
        output = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
            str(path),
        ], text=True, timeout=30).strip()
    else:
        # Pro firmware V1.0.4 ships ffmpeg but not the separate ffprobe binary.
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=30, check=False)
        output = result.stderr
    match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", output)
    if match:
        return int(match.group(1)), int(match.group(2))
    raise RuntimeError(f"could not determine video size for {path}")


def decoded_frames(path: Path):
    width, height = probe_size(path)
    frame_bytes = width * height * 3
    process = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(path), "-an", "-vf", f"fps={FPS:g}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ], stdout=subprocess.PIPE)
    assert process.stdout is not None
    try:
        index = 0
        while True:
            data = process.stdout.read(frame_bytes)
            if not data:
                break
            if len(data) != frame_bytes:
                raise RuntimeError(f"short decoded frame in {path}")
            yield index / FPS, np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
            index += 1
    finally:
        process.stdout.close()
        return_code = process.wait(timeout=10)
        if return_code:
            raise RuntimeError(f"ffmpeg exited {return_code} for {path}")


def trace_row(person, track, obs, timestamp: float, width: int, height: int,
              inference_ms: float) -> dict:
    if person is None or not obs.valid:
        return {
            "timestamp": round(timestamp * 1000.0, 3), "tracking": False,
            "fall_event": False, "pose17": [], "features": {},
            "inference_time_ms": round(inference_ms, 3),
        }
    pose = [[round(float(k[0]) / width, 6), round(float(k[1]) / height, 6),
             round(float(k[2]), 6)] for k in person["keypoints"][:17]]
    return {
        "timestamp": round(timestamp * 1000.0, 3),
        "tracking": True,
        "fall_event": False,
        "track_id": track.track_id,
        "pose17": pose,
        "features": {
            "valid": True,
            "hip_y": round(float(obs.hip_y), 6),
            "torso_angle_deg": round(float(obs.torso_angle_deg), 6),
            "bbox_aspect_ratio": round(float(obs.bbox_aspect_ratio), 6),
            "person_score": round(float(obs.person_score), 6),
        },
        "inference_time_ms": round(inference_ms, 3),
    }


def extract_clip(app, model, source: Path, destination: Path,
                 confidence: float, keypoint_confidence: float) -> tuple[int, float]:
    from kit.runtime.preprocess import preprocess
    from kit.runtime.postprocess.pose import postprocess
    from kit.logic.geometry import make_observation
    import time

    tracker = app.IoUTracker(iou_threshold=0.2, max_lost_sec=0.75)
    rows = []
    for timestamp, frame in decoded_frames(source):
        height, width = frame.shape[:2]
        network_input, info = preprocess(frame, 640)
        started = time.perf_counter()
        outputs = model.infer(network_input)
        inference_ms = (time.perf_counter() - started) * 1000.0
        persons = postprocess(outputs, info, conf_thres=confidence,
                              iou_thres=0.45, kpt_thres=keypoint_confidence)
        tracks = tracker.update(persons, timestamp)
        visible = [track for track in tracks if track.detection_index is not None]
        primary = max(
            visible,
            key=lambda track: float(persons[track.detection_index].get("score", 0.0)),
            default=None,
        )
        person = persons[primary.detection_index] if primary is not None else None
        obs = make_observation(person, timestamp, height, keypoint_confidence)
        rows.append(trace_row(person, primary, obs, timestamp, width, height, inference_ms))
    if not rows:
        raise RuntimeError(f"zero decoded frames: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n"
                               for row in rows), encoding="utf-8")
    partial.replace(destination)
    coverage = sum(bool(row["tracking"]) for row in rows) / len(rows)
    return len(rows), coverage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--subjects", default="1,2,3")
    parser.add_argument("--confidence", type=float, default=0.4)
    parser.add_argument("--keypoint-confidence", type=float, default=0.5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    app_dir = Path(__file__).resolve().parents[1]
    repo = app_dir.parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    app = load_app(app_dir)
    from kit.runtime.engine import RknnModel

    subjects = sorted({int(value) for value in args.subjects.split(",") if value.strip()})
    sources = []
    for subject in subjects:
        for kind in ("ADL", "Fall"):
            for source in sorted((args.dataset / f"subject-{subject}" / kind).glob("*.mp4")):
                destination = args.output / f"subject-{subject}" / kind / f"{source.stem}.jsonl"
                sources.append((source, destination))
    if args.limit:
        sources = sources[:args.limit]
    if not sources:
        raise RuntimeError("no GMDCSA videos found")

    with RknnModel(str(args.model)) as model:
        for index, (source, destination) in enumerate(sources, 1):
            if args.resume and destination.exists() and destination.stat().st_size:
                print(f"[{index}/{len(sources)}] resume {destination}", file=sys.stderr)
                continue
            frames, coverage = extract_clip(
                app, model, source, destination,
                args.confidence, args.keypoint_confidence)
            print(f"[{index}/{len(sources)}] {source} frames={frames} coverage={coverage:.3f}",
                  file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
