#!/usr/bin/env python3
"""
End-to-end detection demo for reCamera Pro (RV1126B). Run ON the device.

    python3 demo_detect.py <model.rknn> <image.jpg> [conf]

Exercises engine.py + preprocess.py + postprocess/detect.py against a real
image and prints detected boxes / classes / scores plus timing.
"""
import sys
import time

from runtime.engine import RknnModel
from runtime.preprocess import preprocess
from runtime.postprocess.detect import postprocess


def main():
    model_path = sys.argv[1]
    img_path = sys.argv[2]
    conf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3

    inp, info = preprocess(img_path, 640)
    model = RknnModel(model_path)

    # warmup
    outs = model.infer(inp)
    t0 = time.time()
    N = 10
    for _ in range(N):
        outs = model.infer(inp)
    dt = (time.time() - t0) / N * 1000

    dets = postprocess(outs, info, conf_thres=conf, iou_thres=0.45)
    model.release()

    print(f"model={model_path}")
    print(f"image={img_path} orig={info.orig_w}x{info.orig_h}")
    print(f"output_shapes={[o.shape for o in outs]}")
    print(f"infer_latency={dt:.1f} ms/frame  (avg of {N})")
    print(f"detections (conf>={conf}): {len(dets)}")
    for d in dets:
        print(f"  {d['cls_name']:<14} score={d['score']:.3f} box={d['box']}")


if __name__ == "__main__":
    main()
