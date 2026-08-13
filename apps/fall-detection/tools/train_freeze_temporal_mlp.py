#!/usr/bin/env python3
"""Train and freeze a reCamera Pro pose-native temporal MLP.

The two commands enforce the subject firewall:

* ``freeze`` reads Subjects 1-3 only: S1-2 fit, S3 selects, S1-3 refit.
* ``test`` reuses the immutable checkpoint and reads clean S4 only.

Torch/Ultralytics/sklearn are training-host dependencies only.  The exported
gzip JSON is consumed by the production app using NumPy alone.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_training(path: Path):
    spec = importlib.util.spec_from_file_location("recamera_temporal_training", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import training module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def default_training_module() -> Path:
    recamera_root = Path(__file__).resolve().parents[4]
    return (recamera_root / "sscma-example-sg200x" / "solutions" /
            "fall-detection" / "tools" / "train_temporal_model.py")


def trace_inventory(root: Path, subjects: set[int]) -> list[dict]:
    rows = []
    for path in sorted(root.glob("subject-*/*/*.jsonl")):
        try:
            subject = int(path.parts[-3].split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if subject in subjects:
            rows.append({"path": path.relative_to(root).as_posix(), "sha256": sha256(path)})
    return rows


def load_subject_clips(training, traces: Path, dataset: Path,
                       subjects: set[int]):
    """Load only the named subjects, including their onset CSVs.

    The shared legacy loader preloads onset CSVs for Subjects 1-4 before it
    filters clips.  That is harmless for model fitting but violates our stricter
    audit rule that the freeze phase must not even read S4 metadata.
    """
    onset_pattern = re.compile(r"fall(?:ing)?[^\[]*\[\s*([0-9]+(?:\.[0-9]+)?)", re.I)
    onsets = {}
    for subject in sorted(subjects):
        csv_path = dataset / f"subject-{subject}" / "Fall.csv"
        if not csv_path.exists():
            continue
        for raw in csv_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[1:]:
            name = raw.split(",", 1)[0].strip()
            if not re.fullmatch(r"\d{2}\.mp4", name):
                continue
            starts = [float(match.group(1)) for match in onset_pattern.finditer(raw)]
            if starts:
                onsets[(subject, name)] = min(starts)

    clips = []
    for path in sorted(traces.glob("subject-*/*/*.jsonl")):
        match = re.search(r"subject-(\d+)/(ADL|Fall)/(\d{2})\.jsonl$", path.as_posix())
        if not match:
            continue
        subject, kind, number = int(match.group(1)), match.group(2), match.group(3)
        if subject not in subjects:
            continue
        frames, heuristic_trigger_sec = training.read_trace(path)
        duration = len(frames) / training.FPS
        label = int(kind == "Fall")
        onset = onsets.get((subject, f"{number}.mp4"), duration * 0.35) if label else math.inf
        clips.append(training.Clip(
            path, subject, label, onset, frames, heuristic_trigger_sec))
    return clips


def export_profile(path: Path, scaler, model, best: dict,
                   frame_mask: np.ndarray, metadata: dict) -> None:
    payload = {
        "schema_version": 1,
        "profile_id": "recamera-pro-rv1126b-yolo11n-pose-v1",
        "pose_frontend": "yolo11n_pose_rawhead_int8.rknn",
        "training_protocol": "S1-2 fit; S3 select; S1-3 refit; S4 unread",
        "window_frames": 48,
        "sample_fps": 15,
        "stride_frames": 3,
        "frame_mask": frame_mask.astype(np.float32).tolist(),
        "mean": scaler.mean_.astype(np.float32).tolist(),
        "scale": scaler.scale_.astype(np.float32).tolist(),
        "w1": model.coefs_[0].astype(np.float32).reshape(-1).tolist(),
        "b1": model.intercepts_[0].astype(np.float32).tolist(),
        "w2": model.coefs_[1].astype(np.float32).reshape(-1).tolist(),
        "b2": float(model.intercepts_[1][0]),
        "threshold": float(best["threshold"]),
        "consecutive": int(best["consecutive"]),
        "metadata": metadata,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    partial.replace(path)


def freeze(args) -> int:
    training = load_training(args.training_module)
    clips = load_subject_clips(training, args.traces, args.dataset, {1, 2, 3})
    counts = {subject: sum(clip.subject == subject for clip in clips) for subject in (1, 2, 3)}
    expected = {subject: training.EXPECTED_SUBJECT_CLIPS[subject] for subject in (1, 2, 3)}
    if counts != expected:
        raise RuntimeError(f"incomplete development traces: {counts}; expected {expected}")
    validation = [clip for clip in clips if clip.subject == 3]

    candidates = []
    for variant, frame_mask in training.FRAME_MASKS.items():
        for hidden in (16, 32):
            for alpha in (1e-3, 1e-2):
                scaler, model = training.fit_model(clips, {1, 2}, hidden, alpha, 2026,
                                                   frame_mask)
                probabilities = {
                    clip.path: training.clip_probability(clip, scaler, model, frame_mask)
                    for clip in validation
                }
                for threshold in np.arange(0.30, 0.81, 0.05):
                    for consecutive in (1, 2, 3):
                        metrics = training.metrics(validation, probabilities,
                                                   float(threshold), consecutive)
                        candidates.append({
                            "variant": variant, "hidden": hidden, "alpha": alpha,
                            "threshold": float(threshold), "consecutive": consecutive,
                            "validation_f1": metrics["f1"],
                            "validation_balanced_accuracy": 0.5 * (
                                metrics["recall"] + metrics["specificity"]),
                            "validation": metrics,
                        })
    best = max(candidates, key=lambda item: (
        item["validation_f1"], item["validation_balanced_accuracy"],
        item["consecutive"], item["threshold"], -item["hidden"], item["alpha"],
    ))
    mask = training.FRAME_MASKS[best["variant"]]
    scaler, model = training.fit_model(
        clips, {1, 2, 3}, best["hidden"], best["alpha"], 2026, mask)
    inventory = trace_inventory(args.traces, {1, 2, 3})
    metadata = {
        "pose_model_sha256": sha256(args.pose_model),
        "development_trace_count": len(inventory),
    }
    export_profile(args.profile, scaler, model, best, mask, metadata)
    checkpoint = {
        "schema_version": 1,
        "status": "frozen_configuration_s4_unread",
        "profile_id": "recamera-pro-rv1126b-yolo11n-pose-v1",
        "protocol": {
            "fit": "Subjects 1-2",
            "select": "Subject 3",
            "refit": "Subjects 1-3",
            "test": "Subject 4, read only by the test command",
            "discarded_subject4_smoke": sorted(
                f"{kind}/{number}.mp4" for kind, number in training.SMOKE_TEST_CLIPS),
        },
        "pose_model": {"path": args.pose_model.name, "sha256": sha256(args.pose_model)},
        "profile": {"path": args.profile.name, "sha256": sha256(args.profile)},
        "clips_by_subject": counts,
        "development_traces": inventory,
        "best": best,
        "subject4_clean_test": None,
    }
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    args.checkpoint.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"best": best, "profile_sha256": sha256(args.profile)}, indent=2))
    return 0


def load_profile(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def profile_probabilities(training, clips, profile: dict) -> dict:
    mask = np.asarray(profile["frame_mask"], dtype=np.float32)
    mean = np.asarray(profile["mean"], dtype=np.float32)
    scale = np.asarray(profile["scale"], dtype=np.float32)
    hidden = len(profile["b1"])
    w1 = np.asarray(profile["w1"], dtype=np.float32).reshape(mean.size, hidden)
    b1 = np.asarray(profile["b1"], dtype=np.float32)
    w2 = np.asarray(profile["w2"], dtype=np.float32)
    b2 = float(profile["b2"])
    output = {}
    for clip in clips:
        x, _ = training.clip_windows(clip, False, mask)
        normalized = (x - mean) / np.maximum(scale, 1e-12)
        activation = np.maximum(0.0, normalized @ w1 + b1)
        logits = activation @ w2 + b2
        probability = np.empty_like(logits, dtype=np.float64)
        nonnegative = logits >= 0.0
        probability[nonnegative] = 1.0 / (1.0 + np.exp(-logits[nonnegative]))
        exp_logits = np.exp(logits[~nonnegative])
        probability[~nonnegative] = exp_logits / (1.0 + exp_logits)
        output[clip.path] = probability
    return output


def test_frozen(args) -> int:
    training = load_training(args.training_module)
    checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
    if checkpoint.get("status") != "frozen_configuration_s4_unread":
        raise RuntimeError("checkpoint is not an S4-unread frozen configuration")
    if sha256(args.profile) != checkpoint["profile"]["sha256"]:
        raise RuntimeError("profile checksum differs from frozen checkpoint")
    if sha256(args.pose_model) != checkpoint["pose_model"]["sha256"]:
        raise RuntimeError("pose model checksum differs from frozen checkpoint")

    clips = load_subject_clips(training, args.traces, args.dataset, {4})
    count = sum(clip.subject == 4 for clip in clips)
    if count != training.EXPECTED_SUBJECT_CLIPS[4]:
        raise RuntimeError(f"incomplete Subject 4 traces: {count}; expected 37")
    clean = [clip for clip in clips if
             (clip.path.parent.name, clip.path.stem) not in training.SMOKE_TEST_CLIPS]
    if len(clean) != 27:
        raise RuntimeError(f"expected 27 clean S4 clips, got {len(clean)}")
    profile = load_profile(args.profile)
    probabilities = profile_probabilities(training, clean, profile)
    metrics = training.metrics(
        clean, probabilities, float(profile["threshold"]), int(profile["consecutive"]))
    report = {
        "schema_version": 1,
        "phase": "frozen_test",
        "checkpoint_sha256": sha256(args.checkpoint),
        "profile_sha256": sha256(args.profile),
        "pose_model_sha256": sha256(args.pose_model),
        "subject4_total": count,
        "subject4_clean_test_count": len(clean),
        "subject4_clean_test": metrics,
        "subject4_traces": trace_inventory(args.traces, {4}),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--training-module", type=Path, default=default_training_module())
    common.add_argument("--traces", type=Path, required=True)
    common.add_argument("--dataset", type=Path, required=True)
    common.add_argument("--pose-model", type=Path, required=True)
    common.add_argument("--profile", type=Path, required=True)
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze", parents=[common])
    freeze_parser.add_argument("--checkpoint", type=Path, required=True)
    freeze_parser.set_defaults(func=freeze)
    test_parser = subparsers.add_parser("test", parents=[common])
    test_parser.add_argument("--checkpoint", type=Path, required=True)
    test_parser.add_argument("--report", type=Path, required=True)
    test_parser.set_defaults(func=test_frozen)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
