#!/usr/bin/env python3
"""
Live end-to-end detection demo for reCamera Pro (RV1126B). Run ON the device.

    python3 demo_live.py <model.rknn> [--n 60] [--conf 0.3] [--source ffmpeg|snapshot]
                         [--url rtsp://...] [--every 1]

Chain:  FrameSource (live camera)  ->  preprocess.letterbox  ->  RknnModel.infer
        ->  postprocess.detect  ->  print detections + measured end-to-end fps.

This is the proof that the FrameSource adapter feeds the already-built kit
inference chain: live camera -> YOLO -> boxes.
"""
import argparse
import sys
import time

from adapters.frame_source import open_frame_source, DEFAULT_SUB_STREAM
from runtime.engine import RknnModel
from runtime.preprocess import letterbox
from runtime.postprocess.detect import postprocess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--n", type=int, default=60, help="frames to process")
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--iou", type=float, default=0.45)
    ap.add_argument("--source", default="ffmpeg", choices=["ffmpeg", "snapshot"])
    ap.add_argument("--url", default=DEFAULT_SUB_STREAM)
    ap.add_argument("--every", type=int, default=1, help="process every Nth frame")
    args = ap.parse_args()

    print(f"[demo_live] model={args.model} source={args.source} url={args.url}")
    model = RknnModel(args.model)

    src = open_frame_source(url=args.url, prefer=args.source)
    print(f"[demo_live] frame source opened; processing {args.n} frames...")

    # accumulators (end-to-end = grab + preprocess + infer + postprocess)
    t_grab = t_pre = t_inf = t_post = 0.0
    processed = 0
    warmed = False
    loop_start = None

    fidx = 0
    last_grab = time.monotonic()
    try:
        for frame in src.frames():
            fidx += 1
            now = time.monotonic()
            t_grab += now - last_grab
            if fidx % args.every != 0:
                last_grab = time.monotonic()
                continue

            t0 = time.monotonic()
            padded, info = letterbox(frame.data, 640)
            t1 = time.monotonic()
            outs = model.infer(padded)
            t2 = time.monotonic()
            dets = postprocess(outs, info, conf_thres=args.conf, iou_thres=args.iou)
            t3 = time.monotonic()

            if not warmed:
                # first frame = warmup, don't count in timing
                warmed = True
                print(f"[warmup] output_shapes={[o.shape for o in outs]} "
                      f"frame={frame.w}x{frame.h} fmt={frame.fmt}")
                loop_start = time.monotonic()
                last_grab = time.monotonic()
                continue

            t_pre += t1 - t0
            t_inf += t2 - t1
            t_post += t3 - t2
            processed += 1

            names = ", ".join(
                f"{d['cls_name']}:{d['score']:.2f}@[{int(d['box'][0])},{int(d['box'][1])},"
                f"{int(d['box'][2])},{int(d['box'][3])}]" for d in dets[:6]
            )
            print(f"frame#{processed:03d} dets={len(dets):2d}  {names}")

            last_grab = time.monotonic()
            if processed >= args.n:
                break
    finally:
        src.close()
        model.release()

    if processed and loop_start:
        wall = time.monotonic() - loop_start
        print("\n=== TIMING (avg over %d frames, warmup excluded) ===" % processed)
        print(f"grab       : {t_grab / max(fidx,1) * 1000:6.1f} ms/frame (stream-paced)")
        print(f"preprocess : {t_pre / processed * 1000:6.1f} ms")
        print(f"infer      : {t_inf / processed * 1000:6.1f} ms")
        print(f"postprocess: {t_post / processed * 1000:6.1f} ms")
        comp = (t_pre + t_inf + t_post) / processed * 1000
        print(f"compute-only (pre+inf+post): {comp:6.1f} ms  -> {1000.0/comp:5.1f} fps")
        print(f"end-to-end wall fps (incl. stream pacing): {processed / wall:5.1f} fps")
    else:
        print("No frames processed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
