#!/usr/bin/env python3
"""03-frame-to-inference-to-osd -- 完整闭环：取帧 -> 自带算法 -> 回注 OSD。

这是"方案商自带流水线"的核心示例：
  FrameSource 拿帧  ->  你自己的推理/算法  ->  ResultSink 把结果回注 OSD/RTSP/推送

为了不依赖任何具体模型，这里的"推理"用一个纯 numpy 的**帧差运动检测**占位：
比较相邻两帧的灰度 Y 平面，把变化明显的区域框出来。你把 `detect()` 换成
自己的模型推理即可（输入 frame.array 或 frame.to_bgr()，输出若干框）。

>>> 占位逻辑标注：detect() 是演示用的帧差运动检测，不是真实模型。替换它。 <<<

关键点：
  - 跨帧要保留的数据必须 .copy()（frame.array 是零拷贝视图，下一帧会被覆写）。
  - 回注时传 pts_us=frame.pts_us，让 OSD 按就近帧对齐叠加。

API 逐一核实自 sdk/librecamera_ext/python/recamera_ext/__init__.py：
  - FrameSource() / for frame in src / frame.array / frame.pts_us  (L470-511, L441-449, L401)
  - ResultSink(source_id).send_detections(pts_us, boxes)           (L232, L241)

运行：  python3 motion_to_osd.py [--source-id motion] [--thresh 25] [--min-pixels 800]
需要：  numpy；设备上有 librecamera_ext.so.1 + 含扩展 API 的固件。
"""
import argparse

import numpy as np

from recamera_ext import FrameSource, ResultSink


def detect(prev_y, cur_y, thresh, min_pixels):
    """占位"推理"：帧差运动检测。返回 [(x1,y1,x2,y2,score,label), ...]。

    prev_y / cur_y: 灰度 Y 平面，形状 (H, W) uint8。
    真实项目里把这里换成你的模型 forward + 后处理，输出同样格式的框。
    """
    diff = np.abs(cur_y.astype(np.int16) - prev_y.astype(np.int16))
    mask = diff > thresh
    ys, xs = np.nonzero(mask)
    if xs.size < min_pixels:
        return []                       # 变化太小，认为无运动
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    # 用变化像素占比当一个粗略"置信度"，仅示意。
    score = min(1.0, xs.size / float(cur_y.size) * 5.0)
    return [(x1, y1, x2, y2, round(score, 2), "motion")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", default="motion")
    ap.add_argument("--thresh", type=int, default=25, help="像素差阈值")
    ap.add_argument("--min-pixels", type=int, default=800, help="判定运动的最小变化像素数")
    args = ap.parse_args()

    prev = None
    with FrameSource() as src, ResultSink(source_id=args.source_id) as sink:
        print("running: %dx%d source_id=%r  (Ctrl-C 退出)"
              % (src.width, src.height, args.source_id))
        for frame in src:
            cur = frame.array                      # 零拷贝 Y 平面视图 (H, W)
            if prev is not None:
                boxes = detect(prev, cur, args.thresh, args.min_pixels)
                if boxes:
                    # pts_us=frame.pts_us -> OSD 按就近帧对齐叠加。
                    sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
                    x1, y1, x2, y2, sc, _ = boxes[0]
                    print("motion seq=%d pts=%d box=(%d,%d,%d,%d) score=%.2f"
                          % (frame.seq, frame.pts_us, x1, y1, x2, y2, sc))
            # 必须 .copy()：cur 是零拷贝视图，下一帧迭代开始后会被覆写。
            prev = cur.copy()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
